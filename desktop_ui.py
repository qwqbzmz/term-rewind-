"""Main desktop client layout."""

from pathlib import Path
import tkinter as tk
from tkinter import ttk


BG = "#f3f4f8"
PANEL = "#ffffff"
EDGE = "#e6e8f1"
INK = "#24243a"
MUTED = "#686c84"
PURPLE = "#554ad3"
NAV = "#1f2037"
NAV_TEXT = "#bfc0d7"
FONT = "Microsoft YaHei UI"

PAGES = (
    ("overview", "项目总览", "\ue80f"),
    ("history", "操作记录", "\ue823"),
    ("settings", "设置", "\ue713"),
)


class DesktopView:
    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.page = None
        self.pages = {}
        self.nav = {}
        self.actions = {}
        self.project_title = tk.StringVar(value="选择一个项目")
        self.project_folder = tk.StringVar(value="尚未打开项目")
        self.float_status = tk.StringVar(value="浮窗关闭")
        self.styles()
        self.build()
        self.navigate("overview")

    def styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(FONT, -13), background=BG, foreground=INK)
        style.configure("TButton", background=PANEL, bordercolor="#dcdfee",
                        lightcolor=PANEL, darkcolor=PANEL, padding=(12, 7), focuscolor=PURPLE)
        style.map("TButton", background=[("active", "#f0effc")],
                  foreground=[("disabled", "#9295a7")])
        style.configure("Primary.TButton", background=PURPLE, foreground="white",
                        bordercolor=PURPLE, lightcolor=PURPLE, darkcolor=PURPLE)
        style.map("Primary.TButton", background=[("disabled", "#e7e5f1"), ("active", "#463ab9")],
                  foreground=[("disabled", "#77748d")])
        style.configure("App.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=INK, rowheight=36, borderwidth=0, relief="flat")
        style.map("App.Treeview", background=[("selected", "#efedff")],
                  foreground=[("selected", "#4338ac")])
        for orient in ("Vertical", "Horizontal"):
            name = f"App.{orient}.TScrollbar"
            style.configure(name, troughcolor=PANEL, background="#e2e3ef", bordercolor=PANEL,
                            lightcolor="#e2e3ef", darkcolor="#e2e3ef", arrowcolor=MUTED, arrowsize=11)
        style.configure("App.TCheckbutton", background=PANEL, foreground=INK,
                        padding=(9, 5), indicatorcolor="#f3f2ff", indicatormargin=(0, 0, 8, 0))
        style.map("App.TCheckbutton", indicatorcolor=[("selected", "#b8afff")])

    def label(self, parent, text="", *, bg=BG, fg=INK, size=13, bold=False, variable=None, **options):
        content = {"textvariable": variable} if variable is not None else {"text": text}
        return tk.Label(parent, bg=bg, fg=fg, font=(FONT, -size, "bold" if bold else "normal"),
                        anchor="w", **content, **options)

    def button(self, parent, text, action, *, key=None, primary=False, variable=None):
        content = {"textvariable": variable} if variable is not None else {"text": text}
        button = ttk.Button(parent, command=action, style="Primary.TButton" if primary else "TButton", **content)
        if key:
            self.actions.setdefault(key, []).append(button)
        return button

    def build(self):
        self.root.configure(bg=BG)
        side = tk.Frame(self.root, bg=NAV, width=188)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=NAV, padx=17, pady=24)
        brand.pack(fill="x")
        mark = tk.Canvas(brand, width=32, height=32, bg=NAV, highlightthickness=0)
        mark.pack(side="left")
        mark.create_oval(1, 1, 31, 31, fill="#7668e9", outline="")
        mark.create_arc(8, 8, 24, 24, start=66, extent=292, style="arc", outline="white", width=1.8)
        mark.create_line(17, 6, 21, 9, 17, 12, fill="white", width=1.6, capstyle="round")
        self.label(brand, "term-rewind", bg=NAV, fg=PANEL, size=15, bold=True).pack(side="left", padx=(8, 0))
        for key, title, icon in PAGES:
            self.nav_row(side, key, title, icon)
        bottom = tk.Frame(side, bg=NAV, padx=18, pady=19)
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, bg="#35364f", height=1).pack(fill="x", pady=(0, 13))
        self.label(bottom, bg=NAV, fg="#e0e1f3", size=12, variable=self.project_folder).pack(fill="x")
        self.label(bottom, bg=NAV, fg=NAV_TEXT, size=11, variable=self.float_status).pack(fill="x", pady=(5, 0))

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(side="left", fill="both", expand=True)
        self.host = tk.Frame(outer, bg=BG)
        self.host.pack(fill="both", expand=True)
        status_bar = tk.Frame(outer, bg=BG, padx=24, pady=8)
        status_bar.pack(side="bottom", fill="x")
        self.label(status_bar, bg=BG, fg=MUTED, size=11, variable=self.app.status).pack(fill="x")
        for key, build in (("overview", self.overview), ("history", self.history_page), ("settings", self.settings)):
            page = tk.Frame(self.host, bg=BG, padx=24, pady=20)
            self.pages[key] = page
            build(page)

    def nav_row(self, parent, key, title, icon):
        row = tk.Frame(parent, bg=NAV, height=46, takefocus=True, cursor="hand2")
        row.pack(fill="x", padx=10, pady=3)
        row.pack_propagate(False)
        stripe = tk.Frame(row, bg=NAV, width=3)
        stripe.pack(side="left", fill="y", pady=7)
        glyph = tk.Label(row, text=icon, bg=NAV, fg=NAV_TEXT, font=("Segoe MDL2 Assets", -16), width=2)
        glyph.pack(side="left", padx=(9, 8))
        label = self.label(row, title, bg=NAV, fg=NAV_TEXT, bold=True)
        label.pack(side="left")
        self.nav[key] = row, stripe, glyph, label
        for widget in (row, glyph, label):
            widget.bind("<Button-1>", lambda event, destination=key: self.navigate(destination))
        row.bind("<Return>", lambda event: self.navigate(key))
        row.bind("<FocusIn>", lambda event: self.paint_nav(key, True))
        row.bind("<FocusOut>", lambda event: self.paint_nav(key))
        row.bind("<Enter>", lambda event: self.paint_nav(key, True))
        row.bind("<Leave>", lambda event: self.paint_nav(key))

    def paint_nav(self, key, hover=False):
        row, stripe, icon, label = self.nav[key]
        selected = key == self.page
        shade = "#39355c" if selected else "#2b2c46" if hover else NAV
        row.configure(bg=shade)
        stripe.configure(bg="#a799ff" if selected else shade)
        for child in (icon, label):
            child.configure(bg=shade, fg="white" if selected else NAV_TEXT)

    def navigate(self, key):
        if self.page:
            self.pages[self.page].pack_forget()
        self.page = key
        self.pages[key].pack(fill="both", expand=True)
        for item in self.nav:
            self.paint_nav(item)
        if key == "overview":
            self.render_dashboard()

    def header(self, parent, title, subtitle):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 16))
        actions = tk.Frame(row, bg=BG)
        actions.pack(side="right")
        text = tk.Frame(row, bg=BG)
        text.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.label(text, title, size=25, bold=True).pack(fill="x")
        if isinstance(subtitle, tk.StringVar):
            self.label(text, fg=MUTED, size=12, variable=subtitle, width=1).pack(fill="x", pady=(4, 0))
        else:
            self.label(text, subtitle, fg=MUTED, size=12).pack(fill="x", pady=(4, 0))
        return actions

    def overview(self, page):
        buttons = self.header(page, "项目总览", self.app.project_path)
        self.button(buttons, "打开项目", self.app.open_project, key="open", primary=True).pack(side="right")
        self.button(buttons, "加载示例", self.demo, key="demo").pack(side="right", padx=(0, 8))

        self.banner = tk.Canvas(page, bg=BG, bd=0, highlightthickness=0, height=144)
        self.banner.pack(fill="x", pady=(0, 14))
        self.banner.bind("<Configure>", self.draw_banner)
        metrics = tk.Frame(page, bg=BG)
        metrics.pack(fill="x", pady=(0, 14))
        self.metric_values = []
        for column, label in enumerate(("最近操作", "进行中的记录", "上次操作的文件变更")):
            metrics.columnconfigure(column, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=PANEL, padx=14, pady=10, highlightthickness=1, highlightbackground=EDGE)
            card.grid(row=0, column=column, sticky="ew", padx=(0, 9 if column < 2 else 0))
            number = tk.StringVar(value="0")
            self.metric_values.append(number)
            self.label(card, bg=PANEL, fg=PURPLE, size=22, bold=True, variable=number).pack(fill="x")
            self.label(card, label, bg=PANEL, fg=MUTED, size=11).pack(fill="x", pady=(3, 0))

        card, title = self.card(page, "最近操作", expand=True)
        self.button(title, "全部记录", lambda: self.navigate("history")).pack(side="right")
        self.recent = tk.Frame(card, bg=PANEL, padx=14, pady=5)
        self.recent.pack(fill="both", expand=True)

    def draw_banner(self, event=None):
        canvas = self.banner
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 160:
            return
        canvas.delete("all")
        canvas.create_polygon(0, 18, 0, 0, 18, 0, width - 18, 0, width, 0, width, 18,
                              width, height - 18, width, height, width - 18, height, 18, height, 0, height,
                              0, height - 18, smooth=True, splinesteps=22, fill="#2f2a57")
        # Draw the geometry inside the card, keeping the main heading clear.
        for index, shade in enumerate(("#4c437c", "#5a4d90", "#6959a5", "#7d6abb")):
            right, top = width - 26 - index * 18, 12 + index * 13
            canvas.create_arc(right - 146 + index * 21, top, right, height - top,
                              start=14 + index * 15, extent=283, style="arc", outline=shade, width=1.4)
        canvas.create_text(22, 50, text=self.project_title.get(), anchor="w",
                           fill="white", font=(FONT, -26, "bold"), width=max(100, width - 210))
        note = "选择目录后，就能记录操作、查看差异和恢复文件。" if not self.app.project else "查看最近改动，或为下一条命令保存快照。"
        canvas.create_text(23, 92, text=note, anchor="w", fill="#dad5f2", font=(FONT, -13))

    def demo(self):
        self.app.load_demo(completed=lambda identifier: self.navigate("history"))

    def card(self, parent, title, *, expand=False, grid=None, subtitle=None):
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
        if grid:
            outer.grid(**grid)
        else:
            outer.pack(fill="both" if expand else "x", expand=expand)
        heading = tk.Frame(outer, bg=PANEL, padx=13, pady=10)
        heading.pack(fill="x")
        left = tk.Frame(heading, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        self.label(left, title, bg=PANEL, bold=True, size=14).pack(fill="x")
        if subtitle is not None:
            self.label(left, bg=PANEL, fg=MUTED, size=11, variable=subtitle).pack(fill="x", pady=(3, 0))
        tk.Frame(outer, bg=EDGE, height=1).pack(fill="x")
        return outer, heading

    def history_page(self, page):
        buttons = self.header(page, "操作记录", self.app.project_path)
        self.button(buttons, "运行命令", self.run, key="run", primary=True).pack(side="right")
        self.button(buttons, "取消命令", self.app.cancel_command, key="cancel").pack(side="right", padx=(0, 8))
        self.button(buttons, "", self.app.toggle_task, key="task", variable=self.app.task_text).pack(side="right", padx=8)
        self.button(buttons, "刷新", self.app.refresh, key="refresh").pack(side="right")

        grid = tk.Frame(page, bg=BG)
        grid.pack(fill="both", expand=True)
        for column, (weight, minimum) in enumerate(((4, 160), (4, 145), (8, 250))):
            grid.columnconfigure(column, weight=weight, minsize=minimum)
        grid.rowconfigure(0, weight=1)
        options = {"row": 0, "sticky": "nsew"}
        records, _ = self.card(grid, "操作时间线", grid={**options, "column": 0, "padx": (0, 10)})
        filter_bar = tk.Frame(records, bg=PANEL, padx=10, pady=7)
        filter_bar.pack(fill="x")
        self.search_entry = ttk.Entry(filter_bar, textvariable=self.app.search_query)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Checkbutton(
            filter_bar, text="仅失败", variable=self.app.only_failed,
            style="App.TCheckbutton",
        ).pack(side="right")
        tk.Frame(records, bg=EDGE, height=1).pack(fill="x")
        records_footer = tk.Frame(records, bg=PANEL, padx=10, pady=6)
        records_footer.pack(side="bottom", fill="x")
        self.label(records_footer, bg=PANEL, fg=MUTED, size=11, variable=self.app.history_summary).pack(side="left")
        self.button(records_footer, "加载更多", self.app.load_more_records, key="load_more").pack(side="right")
        tk.Frame(records, bg=EDGE, height=1).pack(side="bottom", fill="x")
        self.history = self.tree(records)
        self.history.bind("<<TreeviewSelect>>", self.app.on_record)
        files, _ = self.card(grid, "文件变更", subtitle=self.app.record_info,
                             grid={**options, "column": 1, "padx": (0, 10)})
        self.files = self.tree(files, ("kind",), multiple=True)
        self.files.column("kind", width=54, minwidth=44, stretch=False)
        for name, color in (("新增", "#16775d"), ("删除", "#b04754"), ("跳过", MUTED)):
            self.files.tag_configure(name, foreground=color)
        self.files.bind("<<TreeviewSelect>>", self.app.on_file)
        diff, _ = self.card(grid, "差异预览", subtitle=self.app.preview_title, grid={**options, "column": 2})
        self.preview = self.text_view(diff)

        actions = tk.Frame(page, bg=BG, pady=9)
        actions.pack(fill="x")
        self.button(actions, "恢复所选文件", self.app.restore, key="restore", primary=True).pack(side="right")
        self.button(actions, "命令输出", self.app.show_output, key="output").pack(side="right", padx=8)
        self.label(actions, fg=MUTED, size=11, variable=self.app.selection_info).pack(side="right", padx=8)

    def tree(self, parent, columns=(), *, multiple=False):
        bar = ttk.Scrollbar(parent, style="App.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        tree = ttk.Treeview(parent, columns=columns, show="tree", height=1, style="App.Treeview",
                            selectmode="extended" if multiple else "browse", yscrollcommand=bar.set)
        bar.configure(command=tree.yview)
        tree.column("#0", width=120, minwidth=75, stretch=True)
        tree.pack(fill="both", expand=True)
        return tree

    def text_view(self, parent):
        vertical = ttk.Scrollbar(parent, style="App.Vertical.TScrollbar")
        vertical.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(parent, orient="horizontal", style="App.Horizontal.TScrollbar")
        horizontal.pack(side="bottom", fill="x")
        text = tk.Text(parent, width=1, height=1, wrap="none", bg=PANEL, fg=INK, borderwidth=0,
                       padx=12, pady=11, font=("Cascadia Mono", -13), spacing1=3,
                       yscrollcommand=vertical.set, xscrollcommand=horizontal.set, state="disabled")
        vertical.configure(command=text.yview)
        horizontal.configure(command=text.xview)
        text.pack(fill="both", expand=True)
        text.tag_configure("add", foreground="#12644b", background="#e7f6ed")
        text.tag_configure("remove", foreground="#a13348", background="#fbe8ed")
        text.tag_configure("meta", foreground=PURPLE)
        return text

    def settings(self, page):
        self.header(page, "设置", "管理桌面浮窗和本地快照。")
        card, _ = self.card(page, "桌面")
        row = tk.Frame(card, bg=PANEL, padx=14, pady=13)
        row.pack(fill="x")
        self.float_toggle = ttk.Checkbutton(row, text="启用桌面浮窗", variable=self.app.float_enabled,
                                           command=self.app.toggle_floating, style="App.TCheckbutton")
        self.float_toggle.pack(side="right")
        left = tk.Frame(row, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        self.label(left, "桌面浮窗", bg=PANEL, bold=True).pack(fill="x")
        self.label(left, "开启后，下次启动直接显示小胶囊。", bg=PANEL, fg=MUTED, size=11).pack(fill="x", pady=(5, 0))
        tk.Frame(page, bg=BG, height=13).pack()
        storage, _ = self.card(page, "本地快照")
        text = tk.Frame(storage, bg=PANEL, padx=14, pady=13)
        text.pack(fill="x")
        self.button(text, "清理旧快照", self.app.gc_project, key="gc").pack(side="right")
        left_storage = tk.Frame(text, bg=PANEL)
        left_storage.pack(side="left", fill="x", expand=True)
        self.label(left_storage, "快照存储位置（手动点击右侧清理，保留最近 20 条）", bg=PANEL, bold=True).pack(fill="x")
        self.label(left_storage, str(self.app.settings_path.parent), bg=PANEL, fg=MUTED, size=12).pack(fill="x", pady=(5, 0))
        self.label(left_storage, bg=PANEL, fg=MUTED, size=11, variable=self.app.storage_usage).pack(fill="x", pady=(4, 0))

    def run(self):
        self.app.run_dialog()

    def open_record(self, identifier):
        if identifier not in self.app.records:
            self.app.search_query.set("")
            self.app.only_failed.set(False)
        self.history.selection_set(identifier)
        self.history.see(identifier)
        self.app.on_record()
        self.navigate("history")

    def render_dashboard(self):
        records = self.app.all_records or list(self.app.records.values())
        first = records[0] if records else {}
        for value, count in zip(self.metric_values, (len(records),
                sum(item["state"] == "pending" for item in records), len(first.get("changes", {})))):
            value.set(str(count))
        name = self.app.project.root.name if self.app.project else ""
        self.project_folder.set(name or "尚未打开项目")
        self.project_title.set(name or "选择一个项目")
        self.float_status.set("浮窗开启" if self.app.float_enabled.get() else "浮窗关闭")
        self.draw_banner()
        for child in self.recent.winfo_children():
            child.destroy()
        if not records:
            self.label(self.recent, "打开项目或加载示例后，操作记录会显示在这里。", bg=PANEL, fg=MUTED,
                       size=12).pack(fill="x", pady=13)
        for record in records[:4]:
            row = tk.Frame(self.recent, bg=PANEL, cursor="hand2")
            row.pack(fill="x")
            identifier = record["id"]
            detail = "进行中" if record["state"] == "pending" else f"{len(record.get('changes', {}))} 个文件"
            time = self.label(row, f"{detail}   {record['created'][11:16]}  ›", bg=PANEL, fg=MUTED, size=11)
            time.pack(side="right")
            label = self.label(row, self.short_label(record), bg=PANEL, width=1)
            label.pack(side="left", fill="x", expand=True, pady=6)
            for widget in (row, label, time):
                widget.bind("<Button-1>", lambda event, item=identifier: self.open_record(item))
            tk.Frame(self.recent, bg=EDGE, height=1).pack(fill="x")

    def short_label(self, record):
        label = record["label"]
        if record["kind"] == "command":
            executable, separator, arguments = label.partition(" ")
            if not executable.startswith('"'):
                label = Path(executable).name + separator + arguments
        return label
