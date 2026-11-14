"""In-place forms, history and recovery inside the desktop capsule."""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from floating import ACCENT, PANEL, SUBTLE, WHITE
from rewind import Project, RewindError, change_type, default_storage, file_diff


FIELD = "#101c2f"
SELECT = "#34578c"
FONT = ("Microsoft YaHei UI", -12)
CHANGE_LABEL = {"A": "新增", "M": "修改", "D": "删除"}
SKIP_LABEL = {
    "excluded": "由排除规则跳过。",
    "link or reparse point": "符号链接或目录联接不在快照内。",
    "not a regular file": "不是普通文件。",
    "over 8 MiB": "超过单文件 8 MiB 上限。",
}


class FloatingPages:
    def __init__(self, app, shell):
        self.app = app
        self.shell = shell
        self.frame = tk.Frame(shell.canvas, bg=PANEL)
        self.generation = 0
        style = ttk.Style(self.frame)
        style.configure("Capsule.TButton", font=FONT, padding=(8, 5), background="#253958",
                        foreground=WHITE, bordercolor="#40577d")
        style.map("Capsule.TButton", background=[("active", "#34578c")],
                  foreground=[("disabled", SUBTLE)])
        style.configure("CapsuleAccent.TButton", font=FONT, padding=(8, 5), background="#32588f",
                        foreground=WHITE, bordercolor="#6189cf")
        style.map("CapsuleAccent.TButton", background=[("active", "#416ba8")],
                  foreground=[("disabled", SUBTLE)])
        for orientation in ("Vertical", "Horizontal"):
            name = f"Capsule.{orientation}.TScrollbar"
            style.configure(name, background="#304a6b", troughcolor=FIELD, bordercolor=FIELD,
                            arrowcolor=SUBTLE, lightcolor="#304a6b", darkcolor="#304a6b", arrowsize=11)
            style.map(name, background=[("active", SELECT)])

    def home(self):
        self.generation += 1
        self.shell.set_page()
        self.shell.canvas.focus_set()

    def page(self, title, back=None):
        self.generation += 1
        for child in self.frame.winfo_children():
            child.destroy()
        self.shell.set_page(self.frame, title, back or self.home)
        self.frame.focus_set()
        body = tk.Frame(self.frame, bg=PANEL)
        body.pack(side="top", fill="both", expand=True)
        bar = tk.Frame(self.frame, bg=PANEL)
        bar.pack(side="bottom", fill="x", pady=(9, 0))
        return body, bar

    def caption(self, parent, text, *, accent=False):
        tk.Label(parent, text=text, bg=PANEL, fg=ACCENT if accent else SUBTLE, font=FONT,
                 justify="left", anchor="w", wraplength=274).pack(fill="x", pady=(0, 7))

    def button(self, parent, text, command, *, primary=False):
        button = ttk.Button(parent, text=text, command=command,
                            style="CapsuleAccent.TButton" if primary else "Capsule.TButton")
        button.pack(side="right", padx=(5, 0))
        return button

    def entry(self, parent, value=""):
        field = tk.Entry(parent, bg=FIELD, fg=WHITE, insertbackground=WHITE, selectbackground=SELECT,
                         relief="flat", borderwidth=6, font=FONT)
        field.pack(fill="x", pady=(0, 8), ipady=4)
        field.insert(0, value)
        field.focus_set()
        return field

    def listbox(self, parent, *, multiple=False):
        box = tk.Frame(parent, bg=PANEL)
        box.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(box, style="Capsule.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        rows = tk.Listbox(
            box, bg=FIELD, fg=WHITE, selectbackground=SELECT, selectforeground=WHITE,
            font=FONT, relief="flat", borderwidth=5, highlightthickness=0,
            activestyle="none", exportselection=False, selectmode="extended" if multiple else "browse",
            yscrollcommand=scroll.set,
        )
        scroll.configure(command=rows.yview)
        rows.pack(fill="both", expand=True)
        rows.focus_set()
        return rows

    def text(self, parent, content, *, diff=False):
        box = tk.Frame(parent, bg=PANEL)
        box.pack(fill="both", expand=True)
        vertical = ttk.Scrollbar(box, style="Capsule.Vertical.TScrollbar")
        vertical.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(box, orient="horizontal", style="Capsule.Horizontal.TScrollbar")
        horizontal.pack(side="bottom", fill="x")
        view = tk.Text(
            box, bg=FIELD, fg=WHITE, font=("Cascadia Mono", -12), wrap="none",
            padx=7, pady=6, relief="flat", borderwidth=0, width=1, height=1,
            xscrollcommand=horizontal.set, yscrollcommand=vertical.set,
        )
        horizontal.configure(command=view.xview)
        vertical.configure(command=view.yview)
        view.tag_configure("added", foreground="#a0e6bf", background="#203b39")
        view.tag_configure("removed", foreground="#ffb5bf", background="#452d3d")
        view.tag_configure("range", foreground=ACCENT)
        for line in content.splitlines():
            tag = ""
            if diff:
                if line.startswith(("+++", "---", "@@")):
                    tag = "range"
                elif line.startswith("+"):
                    tag = "added"
                elif line.startswith("-"):
                    tag = "removed"
            view.insert("end", line + "\n", tag)
        view.configure(state="disabled")
        view.pack(fill="both", expand=True)

    def error(self, error, retry):
        body, bar = self.page("操作未完成", retry)
        self.text(body, str(error))
        self.button(bar, "返回", retry, primary=True)

    def work(self, title, start, completed, retry):
        body, _ = self.page(title)
        self.caption(body, "处理中…", accent=True)
        generation = self.generation

        def done(identifier):
            if generation == self.generation:
                completed(identifier)

        def failed(error):
            if generation == self.generation:
                self.error(error, retry)

        start(done, failed)

    def open(self, action):
        record = self.app.record()
        if action == "project":
            self.project()
        elif action == "run":
            self.run()
        elif action == "task":
            self.task()
        elif action == "demo":
            self.work("加载示例", self.app.load_demo,
                      lambda identifier: self.files(identifier, ["draft.md"]), self.home)
        elif action in ("diff", "restore") and record:
            self.files(record["id"])
        elif action == "output" and record:
            self.output(record["id"])
        elif action in ("history", "refresh"):
            if action == "refresh" and not self.app.refresh(failed=lambda error: self.error(error, self.home)):
                return
            self.history()

    def project(self, path=None):
        body, bar = self.page("打开项目")
        self.caption(body, "项目文件夹")
        current = self.app.project.root if self.app.project else ""
        field = self.entry(body, str(current if path is None else path))

        def browse():
            selected = filedialog.askdirectory(parent=self.shell.window, mustexist=True, title="选择项目文件夹")
            if selected:
                field.delete(0, "end")
                field.insert(0, selected)
            field.focus_set()

        def submit():
            value = field.get().strip().strip('"')
            if not value:
                field.focus_set()
                return
            try:
                self.app.set_project(Project(Path(value).expanduser(), default_storage()))
            except (OSError, ValueError, RewindError) as error:
                self.error(error, lambda: self.project(value))
                return
            self.history()

        self.button(bar, "打开", submit, primary=True)
        self.button(bar, "浏览…", browse)
        field.bind("<Return>", lambda event: submit())

    def run(self, command=""):
        body, bar = self.page("运行命令")
        self.caption(body, "在当前项目运行程序并记录文件变化。")
        field = self.entry(body, command)
        self.caption(body, "例如  python.exe clean_data.py")

        def submit():
            line = field.get().strip()
            if line and not self.app.busy:
                self.work("运行命令", lambda done, failed: self.app.run_line(line, done, failed),
                          self.output, lambda: self.run(line))

        self.button(bar, "运行并记录", submit, primary=True)
        field.bind("<Return>", lambda event: submit())

    def task(self, label=""):
        record = self.app.record()
        pending = record and record["state"] == "pending"
        body, bar = self.page("结束记录" if pending else "开始任务")
        if pending:
            identifier = record["id"]
            self.caption(body, record["label"], accent=True)
            self.caption(body, "任务完成后，保存结束时的文件状态。")

            def submit():
                self.work("对比任务文件", lambda done, failed: self.app.end_task(identifier, done, failed),
                          self.files, self.task)

            self.button(bar, "结束记录", submit, primary=True)
            return
        self.caption(body, "输入任务名称，保存当前文件状态。")
        field = self.entry(body, label)

        def submit():
            name = field.get().strip()
            if name and not self.app.busy:
                self.work("保存任务起点", lambda done, failed: self.app.begin_task(name, done, failed),
                          lambda identifier: self.task(), lambda: self.task(name))

        self.button(bar, "开始记录", submit, primary=True)
        field.bind("<Return>", lambda event: submit())

    def history(self):
        body, bar = self.page("操作历史")
        records = list(self.app.records.values())
        if not records:
            self.caption(body, "暂无记录。")
            self.button(bar, "运行命令", self.run, primary=True)
            return
        rows = self.listbox(body)
        active = self.app.record()
        index = 0
        for position, record in enumerate(records):
            status = "记录中" if record["state"] == "pending" else record["created"][11:16]
            rows.insert("end", f"{status}  {record['label']}")
            if active and record["id"] == active["id"]:
                index = position
        rows.selection_set(index)
        rows.see(index)

        def submit():
            chosen = rows.curselection()
            if chosen:
                identifier = records[chosen[0]]["id"]
                self.app.history.selection_set(identifier)
                self.app.on_record()
                self.files(identifier)

        self.button(bar, "查看文件", submit, primary=True)
        rows.bind("<Return>", lambda event: submit())
        rows.bind("<Double-Button-1>", lambda event: submit())

    def files(self, identifier, preselected=()):
        record = self.app.records.get(identifier)
        if record is None:
            self.history()
            return
        body, bar = self.page("文件变更", self.history)
        changes, skipped = record.get("changes", {}), record.get("skipped", {})
        paths = list(changes) + list(skipped)
        if record["state"] == "pending":
            self.caption(body, record["label"], accent=True)
            self.caption(body, "这条记录还在进行中。")
            self.button(bar, "结束记录", self.task, primary=True).configure(
                state="disabled" if self.app.busy else "normal")
            return
        if not paths:
            self.caption(body, "这条记录没有文件变更。")
        else:
            self.caption(body, f"{len(changes)} 个变更。按 Ctrl 可选多个文件。")
            rows = self.listbox(body, multiple=True)
            for index, path in enumerate(paths):
                kind = CHANGE_LABEL[change_type(*changes[path])] if path in changes else "跳过"
                rows.insert("end", f"{kind}  {path}")
                if path in preselected:
                    rows.selection_set(index)
                    rows.see(index)

            def selected():
                return [paths[index] for index in rows.curselection()]

            def preview():
                chosen = selected()
                if chosen:
                    self.preview(identifier, chosen[0], chosen)

            def restore():
                chosen = [path for path in selected() if path in changes]
                if chosen and not self.app.busy:
                    self.confirm(record, chosen)

            restore_button = self.button(bar, "恢复所选", restore, primary=True)
            preview_button = self.button(bar, "查看差异", preview)

            def update(event=None):
                chosen = selected()
                restore_button.configure(state="normal" if any(path in changes for path in chosen)
                                         and not self.app.busy else "disabled")
                preview_button.configure(state="normal" if chosen else "disabled")

            rows.bind("<<ListboxSelect>>", update)
            rows.bind("<Return>", lambda event: preview())
            rows.bind("<Double-Button-1>", lambda event: preview())
            update()
        if identifier in self.app.outputs:
            self.button(bar, "命令输出", lambda: self.output(identifier))

    def preview(self, identifier, path, selected):
        record = self.app.records[identifier]
        back = lambda: self.files(identifier, selected)
        body, bar = self.page("文件差异", back)
        self.caption(body, path, accent=True)
        changes = record.get("changes", {})
        if path in changes:
            try:
                lines = file_diff(self.app.project, path, *changes[path])
            except (OSError, RewindError) as error:
                self.text(body, str(error))
            else:
                self.text(body, "\n".join(lines[1:]), diff=True)
                self.button(bar, "恢复此文件", lambda: self.confirm(record, [path], back),
                            primary=True).configure(state="disabled" if self.app.busy else "normal")
        else:
            reason = record["skipped"][path]
            self.caption(body, SKIP_LABEL.get(reason, reason))
        self.button(bar, "返回文件", back)

    def output(self, identifier):
        body, bar = self.page("命令输出", lambda: self.files(identifier))
        record = self.app.records.get(identifier)
        if record and record["exit_code"] is not None:
            self.caption(body, f"退出码 {record['exit_code']}", accent=True)
        self.text(body, self.app.outputs.get(identifier) or "这条命令没有输出。")
        self.button(bar, "查看文件", lambda: self.files(identifier), primary=True)

    def confirm(self, record, paths, back=None):
        identifier = record["id"]
        back = back or (lambda: self.files(identifier, paths))
        body, bar = self.page("确认恢复", back)
        self.caption(body, f"将这 {len(paths)} 个路径恢复到记录前的状态。")
        self.text(body, "\n".join(paths))

        def force_submit():
            if not self.app.busy:
                self.work("正在强制恢复", lambda done, failed: self.app.undo_files(record, paths, done, failed, force=True),
                          lambda result: self.restored(result, len(paths)), back)

        def on_conflict(error):
            if "Conflicts" not in str(error):
                self.error(error, back)
                return
            c_body, c_bar = self.page("检测到文件冲突", back)
            self.caption(c_body, "目标文件之后又被修改。可强制覆盖（当前内容仍会先归档）。", accent=True)
            self.text(c_body, str(error))
            self.button(c_bar, "强制覆盖", force_submit, primary=True)
            self.button(c_bar, "返回", back)

        def submit():
            if not self.app.busy:
                self.work(
                    "正在恢复",
                    lambda done, failed: self.app.undo_files(record, paths, done, on_conflict),
                    lambda result: self.restored(result, len(paths)),
                    back,
                )

        self.button(bar, "确认恢复", submit, primary=True)
        self.button(bar, "取消", back)

    def restored(self, identifier, count):
        body, bar = self.page("恢复完成")
        self.caption(body, f"已恢复 {count} 个路径。", accent=True)
        self.button(bar, "查看恢复记录", lambda: self.files(identifier), primary=True)
