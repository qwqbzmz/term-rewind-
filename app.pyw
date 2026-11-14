"""Windows desktop interface for term-rewind."""

import ctypes
from ctypes import wintypes
import json
import locale
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from desktop_ui import DesktopView
from floating import FloatingWindow
from float_pages import FloatingPages
from instance import AppInstance
from rewind import Project, RewindError, atomic_write, change_type, default_storage, file_diff, prepare_command, win_path


KINDS = {"A": "新增", "M": "修改", "D": "删除"}


def windows_args(line):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    args = shell.CommandLineToArgvW(line.strip(), ctypes.byref(count))
    if not args:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [args[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(args)


def record_program(project, line, on_start=None, on_output=None, cancel_event=None, on_process=None):
    command = windows_args(line)
    program = prepare_command(project, command)
    with project.lock():
        record = project.start(subprocess.list2cmdline(command), "command")
    if on_start is not None:
        on_start(record)
    code, problem = 1, ""
    chunks = []
    total_chars = 0
    truncated = False
    proc = None
    try:
        proc = subprocess.Popen(
            program, cwd=project.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if on_process is not None:
            on_process(proc)
        encoding = locale.getpreferredencoding(False) or "utf-8"
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, b""):
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    text = raw.decode(encoding, errors="replace")
                chunks.append(text)
                total_chars += len(text)
                while total_chars > 32768 and len(chunks) > 1:
                    removed = chunks.pop(0)
                    total_chars -= len(removed)
                    truncated = True
                if on_output is not None:
                    on_output(record["id"], text)
        code = proc.wait()
    except OSError as error:
        problem = str(error)
    finally:
        if on_process is not None:
            on_process(None)
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        try:
            with project.lock():
                project.finish(record, code, cancelled=cancelled)
        except (OSError, RewindError) as error:
            raise RewindError(f"后置快照失败。起点记录 {record['id']} 已保留。\n{error}") from error
    log = "".join(chunks)
    if truncated:
        log = "仅保留最后约 32 KiB 输出。\n\n" + log
    return record["id"], code, problem or log


def build_demo():
    folder = Path(__file__).resolve().parent / "demo-data"
    if len(str(folder)) > 200:
        folder = default_storage() / "demo-data"
    win_path(folder).mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="demo-", dir=win_path(folder)))
    root = base / "project"
    win_path(root).mkdir()
    win_path(root / "config.json").write_text('{"mode": "original"}\n', encoding="utf-8")
    win_path(root / "draft.md").write_text("# 未提交的草稿\n\n这段内容还没交给 Git。\n", encoding="utf-8")
    win_path(root / "clean_data.py").write_text(
        'from pathlib import Path\n'
        'Path("config.json").write_text(\'{"mode": "broken"}\\n\', encoding="utf-8")\n'
        'Path("draft.md").unlink()\n'
        'Path("generated.tmp").write_text("generated\\n", encoding="utf-8")\n'
        'print("Demo script failed after modifying files.")\n'
        'raise SystemExit(7)\n', encoding="utf-8",
    )
    project = Project(root, base / "snapshots")
    # pythonw runs the interface. The child uses python.exe so its output can be captured.
    python = Path(sys.executable).with_name("python.exe")
    identifier, code, output = record_program(project, subprocess.list2cmdline([str(python), "clean_data.py"]))
    if code != 7:
        raise RewindError(f"示例脚本退出码异常（{code}）。\n{output}")
    win_path(root / "config.json").write_text('{"mode": "edited-afterward"}\n', encoding="utf-8")
    return project, identifier, output


class RewindApp:
    def __init__(self, root, settings_path=None, activation=None):
        self.root = root
        self.activation = activation
        self.settings_path = settings_path or (default_storage() / "desktop.json")
        self.settings = self.load_settings()
        self.floating = None
        self.float_pages = None
        self.float_enabled = tk.BooleanVar(value=self.settings.get("enabled", False))
        self.project = None
        self.all_records = []
        self.records = {}
        self.history_limit = 30
        self.filtered_total = 0
        self.paths = {}
        self.displayed_record = None
        self.outputs = {}
        self.busy = False
        self.results = queue.Queue()
        self.live_queue = queue.Queue()
        self.proc_lock = threading.Lock()
        self.active_proc = None
        self.active_record_id = None
        self.cancel_requested = threading.Event()
        self.search_query = tk.StringVar()
        self.only_failed = tk.BooleanVar(value=False)
        self.history_summary = tk.StringVar(value="0 条记录")
        self.project_path = tk.StringVar(value="尚未选择项目")
        self.status = tk.StringVar(value="选择一个项目，或加载示例。")
        self.preview_title = tk.StringVar(value="差异预览")
        self.record_info = tk.StringVar(value="选择一条历史记录")
        self.selection_info = tk.StringVar()
        self.storage_usage = tk.StringVar(value="打开项目后可查看当前快照占用空间。")
        self.task_text = tk.StringVar(value="开始任务")
        self.root.title("term-rewind")
        icon = Path(__file__).resolve().with_name("term-rewind.ico")
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))
        width = min(1280, root.winfo_screenwidth() - 60)
        height = min(770, root.winfo_screenheight() - 90)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(900, 575)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.ui = DesktopView(self)
        self.history, self.files, self.preview = self.ui.history, self.ui.files, self.ui.preview
        self.search_query.trace_add("write", lambda *_: self.on_filter_change())
        self.only_failed.trace_add("write", lambda *_: self.on_filter_change())
        self.show_text("打开示例项目后，选择一个文件来查看命令执行前后的差异。")
        self.update_buttons()
        self.poll_timer = self.root.after(60, self.poll_work)
        if self.float_enabled.get():
            self.root.after_idle(self.apply_floating)

    def load_settings(self):
        try:
            data = json.loads(win_path(self.settings_path).read_bytes())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        settings = {
            "enabled": data.get("enabled") is True,
            "side": "left" if data.get("side") == "left" else "right",
        }
        vertical = data.get("vertical")
        if isinstance(vertical, (int, float)) and 0 <= vertical <= 1:
            settings["vertical"] = vertical
        monitor = data.get("monitor")
        if (isinstance(monitor, list) and len(monitor) == 2
                and all(type(value) is int and abs(value) < 100000 for value in monitor)):
            settings["monitor"] = monitor
        return settings

    def save_settings(self):
        try:
            atomic_write(self.settings_path, json.dumps(self.settings).encode("utf-8"))
        except OSError:
            self.status.set("设置未能写入本机。")

    def toggle_floating(self):
        self.apply_floating()
        self.settings["enabled"] = self.float_enabled.get()
        self.save_settings()
        self.ui.render_dashboard()

    def apply_floating(self):
        if not self.float_enabled.get():
            if self.floating:
                self.floating.hide()
            return
        try:
            if self.floating is None:
                self.floating = FloatingWindow(
                    self.root, self.settings, self.floating_action, self.save_settings,
                )
                self.float_pages = FloatingPages(self, self.floating)
            self.update_floating()
            self.floating.show()
        except (OSError, tk.TclError) as error:
            self.float_enabled.set(False)
            if self.floating:
                self.floating.hide()
            if self.root.state() == "withdrawn":
                self.show_main()
            self.status.set(f"浮窗启动失败：{error}")

    def disable_floating(self):
        self.float_enabled.set(False)
        self.toggle_floating()

    def reopen(self):
        if self.root.state() == "withdrawn" and self.floating and self.floating.visible:
            self.floating.expand(True)
            self.floating.canvas.focus_force()
        else:
            self.show_main()

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.floating:
            self.floating.expand(False)

    def update_floating(self):
        if self.floating is None:
            return
        name = self.project.root.name if self.project else ""
        record = self.record()
        count = len(record.get("changes", {})) if record else 0
        pending = bool(record and record["state"] == "pending")
        detail = "记录中" if record and record["state"] == "pending" else f"{count} 个变更" if record else ""
        ready = not self.busy
        available = {
            "main": True, "disable": True, "quit": ready,
            "project": ready, "demo": ready,
            "run": ready and bool(self.project), "task": ready and bool(self.project),
            "refresh": ready and bool(self.project),
            "history": bool(self.project), "diff": bool(record),
            "output": bool(record and record["id"] in self.outputs),
            "restore": ready and bool(record and record.get("changes")),
        }
        self.floating.set_content(name, detail, bool(count), available, "结束所选记录" if pending else "开始任务")

    def floating_action(self, action):
        shell_actions = {"main": self.show_main, "disable": self.disable_floating, "quit": self.quit_app}
        if action in shell_actions:
            shell_actions[action]()
        else:
            self.float_pages.open(action)

    def show_text(self, text, *, colored=False):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        for line in text.splitlines():
            tag = ""
            if colored:
                if line.startswith(("+++", "---", "@@")):
                    tag = "meta"
                elif line.startswith("+"):
                    tag = "add"
                elif line.startswith("-"):
                    tag = "remove"
            self.preview.insert("end", line + "\n", tag)
        self.preview.configure(state="disabled")
        self.preview.yview_moveto(0)
        self.preview.xview_moveto(0)

    def record(self):
        selected = self.history.selection()
        return self.records.get(selected[0]) if selected else None

    def selected_paths(self):
        return [self.paths[item][0] for item in self.files.selection()
                if item in self.paths and self.paths[item][1] is None]

    def update_buttons(self):
        record = self.record()
        selected = self.selected_paths()
        pending = record and record["state"] == "pending"
        self.task_text.set("结束所选记录" if pending else "开始任务")
        self.selection_info.set(f"已选 {len(selected)} 个文件" if selected else "")
        with self.proc_lock:
            can_cancel = self.active_proc is not None and self.busy
        available = {
            "open": True, "demo": True, "run": self.project, "task": self.project,
            "refresh": self.project, "restore": selected, "gc": self.project,
            "output": record and record["id"] in self.outputs,
            "load_more": self.project and len(self.records) < self.filtered_total,
        }
        for key, buttons in self.ui.actions.items():
            for button in buttons:
                if key == "cancel":
                    button.configure(state="normal" if can_cancel else "disabled")
                else:
                    button.configure(state="normal" if available.get(key) and not self.busy else "disabled")
        self.update_floating()

    def start_work(self, status, job, done, failed=None):
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        self.update_buttons()

        def worker():
            try:
                self.results.put((done, failed, job(), None))
            except Exception as error:
                self.results.put((done, failed, None, error))

        threading.Thread(target=worker, daemon=True).start()

    def drain_live_queue(self):
        updated_live = False
        while True:
            try:
                event = self.live_queue.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "started":
                rec = event[1]
                self.active_record_id = rec["id"]
                self.outputs[rec["id"]] = ""
                if self.project is not None:
                    self.all_records = [rec] + [r for r in self.all_records if r["id"] != rec["id"]]
                    self.apply_history_filter(identifier=rec["id"])
                    self.preview_title.set("实时输出")
                    self.show_text("命令运行中，正在等待输出…")
                self.update_buttons()
            elif kind == "output":
                rec_id, text = event[1], event[2]
                current = self.outputs.get(rec_id, "") + text
                if len(current) > 65536:
                    current = current[-65536:]
                self.outputs[rec_id] = current
                selected = self.record()
                if selected and selected["id"] == rec_id and not self.files.selection():
                    updated_live = True
        if updated_live and self.active_record_id:
            self.preview_title.set("实时输出")
            self.show_text(self.outputs.get(self.active_record_id, ""))

    def poll_work(self):
        self.poll_timer = None
        if self.activation and self.activation.requested():
            self.reopen()
        self.drain_live_queue()
        try:
            done, failed, value, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            with self.proc_lock:
                self.active_proc = None
            self.active_record_id = None
            if error is None:
                done(value)
            else:
                self.refresh()
                self.status.set("操作未完成")
                if failed:
                    failed(error)
                else:
                    messagebox.showerror("term-rewind", str(error), parent=self.root)
            self.update_buttons()
        self.poll_timer = self.root.after(60, self.poll_work)

    def close(self):
        if self.busy:
            messagebox.showinfo("term-rewind", "正在运行，当前操作结束后可关闭窗口。", parent=self.root)
            return
        if self.floating and self.floating.visible:
            self.root.withdraw()
            return
        self.quit_app()

    def quit_app(self):
        if self.busy:
            messagebox.showinfo("term-rewind", "正在运行，当前操作结束后可退出。", parent=self.root)
            return
        if self.poll_timer is not None:
            self.root.after_cancel(self.poll_timer)
        if self.floating:
            self.floating.destroy()
        self.root.destroy()

    def open_project(self):
        selected = filedialog.askdirectory(parent=self.root, title="选择要保护的项目文件夹", mustexist=True)
        if selected:
            try:
                self.set_project(Project(selected, default_storage()))
            except (OSError, ValueError, RewindError) as error:
                messagebox.showerror("打开项目失败", str(error), parent=self.root)

    def set_project(self, project, identifier=None):
        self.history_limit = 30
        self.all_records = project.records()
        self.project = project
        self.project_path.set(str(project.root))
        self.apply_history_filter(identifier)
        self.status.set("项目已打开")

    def load_demo(self, completed=None, failed=None):
        def done(value):
            project, identifier, output = value
            self.outputs[identifier] = output
            self.set_project(project, identifier)
            for item, (path, reason) in self.paths.items():
                if path == "draft.md":
                    self.files.selection_set(item)
                    self.on_file()
            self.status.set("示例已载入，可以恢复 draft.md。")
            if completed:
                completed(identifier)

        self.start_work("正在准备示例项目…", build_demo, done, failed)

    def refresh(self, identifier=None, failed=None):
        if self.project is None:
            return False
        previous = self.record()
        identifier = identifier or (previous and previous["id"])
        try:
            self.all_records = self.project.records()
        except (OSError, ValueError) as error:
            self.status.set(f"读取历史失败：{error}")
            if failed:
                failed(error)
            return False
        self.apply_history_filter(identifier)
        return True

    def on_filter_change(self):
        if self.project is None:
            return
        self.history_limit = 30
        self.apply_history_filter()

    def load_more_records(self):
        if self.project is None:
            return
        self.history_limit += 30
        self.apply_history_filter()

    def filter_records(self):
        records = self.all_records
        if self.only_failed.get():
            records = [
                rec for rec in records
                if rec.get("state") in ("failed", "corrupt", "partial")
                or bool(rec.get("exit_code"))
                or bool(rec.get("cancelled"))
            ]
        query = self.search_query.get().strip().lower()
        if query:
            records = [
                rec for rec in records
                if query in rec.get("label", "").lower()
                or query in (rec.get("agent") or "").lower()
                or any(query in path.lower() for path in rec.get("changes", {}))
            ]
        return records

    def apply_history_filter(self, identifier=None):
        filtered = self.filter_records()
        self.filtered_total = len(filtered)
        visible = filtered[:self.history_limit]
        self.history_summary.set(f"显示 {len(visible)} / {self.filtered_total} 条")
        self.render_history(visible, identifier)

    def render_history(self, records, identifier):
        self.history.delete(*self.history.get_children())
        self.records = {record["id"]: record for record in records}
        for record in records:
            label = record["label"]
            if record["kind"] == "command":
                parsed = windows_args(label)
                if parsed:
                    program, *args = parsed
                    label = subprocess.list2cmdline([Path(program).name, *args])
            badges = []
            if record.get("agent"):
                step_tag = f"#{record['step']}" if record.get("step") is not None else ""
                badges.append(f"[{record['agent']}{step_tag}]")
            if record.get("concurrent"):
                badges.append("[并行批次]")
            if record.get("cancelled"):
                badges.append("[已取消]")
            elif record["state"] == "pending":
                badges.append("[进行中]")
            elif record.get("exit_code"):
                badges.append(f"[退出 {record['exit_code']}]")
            if badges:
                label = f"{' '.join(badges)} {label}"
            self.history.insert("", "end", iid=record["id"], text="  " + label)
        if self.records:
            self.history.selection_set(identifier if identifier in self.records else next(iter(self.records)))
        self.on_record()
        self.update_storage_info()
        self.ui.render_dashboard()

    def update_storage_info(self):
        if self.project is None:
            self.storage_usage.set("打开项目后可查看当前快照占用空间。")
            return
        try:
            info = self.project.stats()
        except OSError:
            self.storage_usage.set("无法读取当前快照占用空间。")
            return
        size = info["bytes"]
        if size >= 1024 * 1024:
            human = f"{size / (1024 * 1024):.1f} MiB"
        elif size >= 1024:
            human = f"{size / 1024:.1f} KiB"
        else:
            human = f"{size} 字节"
        self.storage_usage.set(
            f"当前项目占用：{info['records']} 条记录，{info['objects']} 个对象，共 {human}（存储目录：{info['store']}）"
        )

    def on_record(self, event=None):
        record = self.record()
        if event is not None and record is self.displayed_record:
            return
        self.displayed_record = record
        self.files.delete(*self.files.get_children())
        self.paths.clear()
        if record is None:
            self.record_info.set("暂无历史")
            self.preview_title.set("差异预览")
            self.show_text("运行一条命令，或开始一次任务记录。")
        else:
            changes = record.get("changes", {})
            skipped = record.get("skipped", {})
            detail = f"{len(changes)} 个变更"
            if record["state"] == "pending":
                detail = "进行中"
            elif record["state"] == "failed":
                detail = "记录失败（未被保护）"
            elif record["state"] == "corrupt":
                detail = "记录损坏"
            elif record["state"] == "partial":
                detail += " · 部分完成"
            elif record.get("cancelled"):
                detail += f" · 已取消 (退出 {record.get('exit_code')})"
            elif record.get("exit_code"):
                detail += f" · 退出 {record['exit_code']}"
            if record.get("batch"):
                detail += f" · 批次 {record['batch']}"
            self.record_info.set(f"{record['created'][5:16].replace('T', ' ')}  ·  {detail}")
            for path, (before, after) in changes.items():
                kind = KINDS[change_type(before, after)]
                item = self.files.insert("", "end", text="  " + path, values=(kind,), tags=(kind,))
                self.paths[item] = (path, None)
            for path, reason in skipped.items():
                item = self.files.insert("", "end", text="  " + path, values=("跳过",), tags=("跳过",))
                self.paths[item] = (path, reason)
            children = self.files.get_children()
            if children:
                self.files.selection_set(children[0])
        self.on_file()

    def on_file(self, event=None):
        record = self.record()
        selected = self.files.selection()
        if record and selected and selected[0] in self.paths:
            path, reason = self.paths[selected[0]]
            self.preview_title.set(path)
            if reason:
                explanations = {
                    "excluded": "由排除规则跳过。",
                    "link or reparse point": "符号链接或目录联接不在快照内。",
                    "not a regular file": "不是普通文件。",
                    "over 8 MiB": "超过单文件 8 MiB 上限。",
                }
                self.show_text(explanations.get(reason, reason))
            else:
                try:
                    lines = file_diff(self.project, path, *record["changes"][path])
                    self.show_text("\n".join(lines[1:]), colored=True)
                except (OSError, RewindError) as error:
                    self.show_text(str(error))
        elif record:
            self.preview_title.set("差异预览")
            if record["state"] in ("failed", "corrupt"):
                self.show_text(record.get("error") or "该记录未成功保存快照。")
            elif record["state"] == "pending" and record["id"] in self.outputs:
                self.preview_title.set("实时输出")
                self.show_text(self.outputs[record["id"]] or "命令运行中，正在等待输出…")
            else:
                self.show_text("记录进行中。" if record["state"] == "pending" else "没有可显示的文件变更。")
        self.update_buttons()

    def run_dialog(self):
        line = simpledialog.askstring(
            "运行并记录命令",
            "命令会在当前项目中执行。请输入程序和参数。\n例如 python.exe clean_data.py",
            parent=self.root,
        )
        if line and line.strip():
            self.run_line(line)

    def run_line(self, line, completed=None, failed=None):
        project = self.project
        self.cancel_requested.clear()

        def on_start(rec):
            self.live_queue.put(("started", rec))

        def on_output(rec_id, text):
            self.live_queue.put(("output", rec_id, text))

        def on_process(proc):
            with self.proc_lock:
                self.active_proc = proc

        def done(value):
            identifier, code, output = value
            self.outputs[identifier] = output
            self.refresh(identifier)
            if self.cancel_requested.is_set():
                self.status.set(f"命令已取消（退出码 {code}），已保留产生的文件变更")
            else:
                self.status.set(f"命令已记录，退出码 {code}")
            if completed:
                completed(identifier)

        self.start_work(
            "正在运行并实时记录命令…",
            lambda: record_program(
                project, line, on_start=on_start, on_output=on_output,
                cancel_event=self.cancel_requested, on_process=on_process,
            ),
            done,
            failed,
        )

    def cancel_command(self):
        self.cancel_requested.set()
        with self.proc_lock:
            proc = self.active_proc
        if proc is None:
            return
        self.status.set("正在取消命令并保存已产生的变更…")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.terminate()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def toggle_task(self):
        record = self.record()
        if record and record["state"] == "pending":
            self.end_task(record["id"])
            return
        label = simpledialog.askstring("开始任务", "输入任务名称，例如 Agent 修复登录问题。", parent=self.root)
        if label and label.strip():
            self.begin_task(label)

    def begin_task(self, label, completed=None, failed=None):
        project = self.project

        def work():
            with project.lock():
                return project.start(label.strip(), "task")["id"]

        def done(identifier):
            self.refresh(identifier)
            self.status.set("起点已保存。任务完成后选中该记录并结束。")
            if completed:
                completed(identifier)

        self.start_work("正在保存任务起点…", work, done, failed)

    def end_task(self, identifier, completed=None, failed=None):
        project = self.project

        def work():
            with project.lock():
                current = project.get(identifier)
                if current["state"] != "pending":
                    return identifier
                if "planned" in current:
                    return project.recover_undo(current)["id"]
                return project.finish(current)["id"]

        def done(result):
            self.refresh(result)
            self.status.set("任务已记录")
            if completed:
                completed(result)

        self.start_work("正在对比任务前后的文件…", work, done, failed)

    def restore(self):
        record, paths = self.record(), self.selected_paths()
        if not record or not paths:
            return
        listed = "\n".join(paths[:8])
        if len(paths) > 8:
            listed += f"\n另有 {len(paths) - 8} 个文件"
        if not messagebox.askyesno(
            "恢复所选文件", f"将这 {len(paths)} 个路径恢复到记录开始前的状态？\n\n{listed}",
            parent=self.root,
        ):
            return

        def on_failed(error):
            text = str(error)
            if "Conflicts" in text and messagebox.askyesno(
                "检测到文件冲突",
                f"{text}\n\n是否强制覆盖这些文件的当前内容？（当前内容也会先归档，后续仍可撤销）",
                parent=self.root,
            ):
                self.undo_files(record, paths, force=True)
                return
            messagebox.showerror("term-rewind", text, parent=self.root)

        self.undo_files(record, paths, failed=on_failed)

    def undo_files(self, record, paths, completed=None, failed=None, force=False):
        project = self.project

        def work():
            with project.lock():
                return project.undo(record, paths, force=force)["id"]

        def done(identifier):
            self.refresh(identifier)
            self.status.set(f"已恢复 {len(paths)} 个文件")
            if self.floating:
                self.floating.feedback()
            if completed:
                completed(identifier)

        self.start_work("正在检查并恢复所选文件…", work, done, failed)

    def gc_project(self):
        project = self.project
        if project is None or self.busy:
            return

        def work():
            with project.lock():
                return project.gc(keep=20)

        def done(result):
            removed_records, removed_objects, freed = result
            self.refresh()
            message = f"已清理 {removed_records} 条旧记录、{removed_objects} 个对象（释放 {freed} 字节）。"
            self.status.set(message)
            messagebox.showinfo("清理旧快照", message, parent=self.root)

        self.start_work("正在清理旧快照…", work, done)

    def show_output(self):
        record = self.record()
        if record and record["id"] in self.outputs:
            self.preview_title.set("命令输出")
            self.show_text(self.outputs[record["id"]] or "这条命令没有输出。")


def main():
    with AppInstance() as activation:
        if not activation.primary:
            return
        root = tk.Tk()
        root.withdraw()
        app = RewindApp(root, activation=activation)
        if not app.float_enabled.get():
            root.deiconify()
        if "--demo" in sys.argv[1:]:
            root.after(0, app.load_demo)
        root.mainloop()


if __name__ == "__main__":
    main()
