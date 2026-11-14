"""Optional desktop launcher for the Windows app."""

import ctypes
from ctypes import wintypes
import math
import time
import tkinter as tk
from tkinter import font


KEY = "#ff00fb"
PANEL = "#172339"
BORDER = "#40577d"
WHITE = "#eff4ff"
SUBTLE = "#a6badc"
ACCENT = "#91afff"
WIDE, TALL = 320, 352
SMALL_W, SMALL_H = 106, 48
PAD, GAP = 6, 10
TILES = (
    ("project", "打开项目", "folder"), ("demo", "加载示例", "spark"),
    ("run", "运行命令", "terminal"), ("task", "开始任务", "flag"),
    ("history", "查看历史", "history"), ("diff", "查看差异", "diff"),
    ("output", "命令输出", "output"), ("restore", "恢复所选文件", "restore"),
)
FOOTER = (
    ("main", "主窗口"), ("refresh", "刷新"), ("disable", "关闭浮窗"), ("quit", "退出"),
)


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("monitor", wintypes.RECT),
        ("work", wintypes.RECT), ("flags", wintypes.DWORD),
    ]


def ease_out(value):
    return 1 - (1 - value) ** 4


def ease_in_out(value):
    return 4 * value ** 3 if value < 0.5 else 1 - (-2 * value + 2) ** 3 / 2


def ease_back(value):
    t = value - 1
    return 1 + 2.15 * t ** 3 + 1.15 * t ** 2


def mix(first, second, weight):
    return first + (second - first) * weight


def blend(first, second, weight):
    channels = (2, 4, 6)
    rgb = [
        round(mix(int(first[i - 1:i + 1], 16), int(second[i - 1:i + 1], 16), weight))
        for i in channels
    ]
    return "#%02x%02x%02x" % tuple(rgb)


def rounded(canvas, x, y, width, height, radius, color):
    right, bottom = x + width, y + height
    canvas.create_polygon(
        x, y + radius, x, y, x + radius, y, right - radius, y, right, y,
        right, y + radius, right, bottom - radius, right, bottom,
        right - radius, bottom, x + radius, bottom, x, bottom, x, bottom - radius,
        smooth=True, splinesteps=20, fill=color,
    )


def draw_icon(canvas, kind, x, y, color):
    def line(*points, **options):
        coordinates = [value + (x if index % 2 == 0 else y) for index, value in enumerate(points)]
        canvas.create_line(*coordinates, fill=color, width=1.4, capstyle="round", joinstyle="round", **options)

    if kind == "folder":
        line(-7, 5, -7, -5, -2, -5, 0, -3, 7, -3, 7, 5, -7, 5)
    elif kind == "spark":
        line(0, -7, 2, -2, 7, 0, 2, 2, 0, 7, -2, 2, -7, 0, -2, -2, 0, -7)
    elif kind == "terminal":
        line(-6, -4, -2, 0, -6, 4)
        line(0, 4, 6, 4)
    elif kind == "flag":
        line(-5, 7, -5, -6, 5, -6, 3, -2, 5, 1, -5, 1)
    elif kind == "history":
        canvas.create_arc(x - 7, y - 7, x + 7, y + 7, start=0, extent=310, style="arc", outline=color, width=1.4)
        line(7, -4, 7, 0, 3, 0)
        line(0, -4, 0, 0, -3, 2)
    elif kind == "diff":
        line(-7, -3, -1, -3)
        line(-4, -6, -4, 0)
        line(1, 4, 7, 4)
    elif kind == "output":
        for row in (-4, 0, 4):
            line(-6, row, 6 if row != 0 else 3, row)
    elif kind == "restore":
        line(-1, -5, -6, -1, -1, 3)
        line(-6, -1, 2, -1, 5, 1, 5, 5)


class Motion:
    def __init__(self, window, enabled):
        self.window = window
        self.enabled = enabled
        self.tracks = {}
        self.timer = None

    def to(self, key, current, target, duration, update, easing=ease_out, done=None):
        self.tracks.pop(key, None)
        if not self.enabled or current == target:
            update(target)
            if done:
                done()
            return
        self.tracks[key] = (time.monotonic(), duration, current, target, easing, update, done)
        if self.timer is None:
            self.timer = self.window.after(0, self.tick)

    def cancel(self, key):
        self.tracks.pop(key, None)

    def stop(self):
        self.tracks.clear()
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None

    def tick(self):
        self.timer = None
        now = time.monotonic()
        for key, (start, duration, current, target, easing, update, done) in tuple(self.tracks.items()):
            progress = min(1, (now - start) / duration)
            if progress == 1:
                del self.tracks[key]
            update(tuple(mix(a, b, easing(progress)) for a, b in zip(current, target)))
            if progress == 1 and done:
                done()
        if self.tracks and self.timer is None:
            self.timer = self.window.after(15, self.tick)


class Desktop:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.api.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        self.api.MonitorFromPoint.restype = wintypes.HANDLE
        self.api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.api.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.UINT,
        ]
        self.api.SystemParametersInfoW.argtypes = [
            wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
        ]

    def cursor(self):
        point = wintypes.POINT()
        self.api.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def work_area(self, point):
        monitor = self.api.MonitorFromPoint(wintypes.POINT(*point), 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not self.api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.work

    def move(self, handle, point):
        # Keep the existing size, stacking order and keyboard focus.
        self.api.SetWindowPos(handle, None, *map(round, point), 0, 0, 0x0015)

    def animations_enabled(self):
        enabled = wintypes.BOOL()
        success = self.api.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return bool(enabled.value) if success else True


class FloatingWindow:
    def __init__(self, root, settings, invoke, save_position):
        self.desktop = Desktop()
        self.settings = settings
        self.invoke = invoke
        self.save_position = save_position
        self.side = settings.get("side", "right")
        self.opens_up = False
        self.window = tk.Toplevel(root, bg=KEY)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-transparentcolor", KEY)
        self.window.attributes("-alpha", 0.0)
        self.window.geometry(f"{WIDE}x{TALL}+0+0")
        self.canvas = tk.Canvas(self.window, bg=KEY, bd=0, highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        # Pixel fonts keep the text within the drawn controls at Windows display scaling.
        self.heading = font.Font(root=root, family="Microsoft YaHei UI", size=-14, weight="bold")
        self.small = font.Font(root=root, family="Microsoft YaHei UI", size=-11)
        self.action_font = font.Font(root=root, family="Microsoft YaHei UI", size=-12, weight="bold")
        self.window.update_idletasks()
        self.handle = int(self.window.tk.call("wm", "frame", self.window._w), 16)
        self.motion = Motion(self.window, self.desktop.animations_enabled())
        self.visible = False
        self.expanded = False
        self.progress = 0.0
        self.reveal = 0.0
        self.action_hover = 0.0
        self.hovered = None
        self.focused = None
        self.alpha = 0.0
        self.pulse = 0.0
        self.slide = 0.0
        self.xy = (0.0, 0.0)
        self.project = ""
        self.detail = ""
        self.has_changes = False
        self.available = {}
        self.task_label = "开始任务"
        self.page = None
        self.page_title = ""
        self.page_back = None
        self.press = None
        self.dragged = False
        self.menu = tk.Menu(self.window, tearoff=False)
        self.menu.add_command(label="打开 term-rewind", command=lambda: self.activate("main"))
        self.menu.add_command(label="关闭桌面浮窗", command=lambda: self.activate("disable"))
        self.menu.add_separator()
        self.menu.add_command(label="退出 term-rewind", command=lambda: self.activate("quit"))
        self.canvas.bind("<Motion>", self.pointer_moved)
        self.canvas.bind("<Leave>", lambda event: self.set_hover(None))
        self.canvas.bind("<ButtonPress-1>", self.press_at)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", self.context_menu)
        self.canvas.bind("<Return>", self.keyboard_activate)
        self.window.bind("<Escape>", self.escape)
        for key in ("Tab", "Down", "Right"):
            self.canvas.bind(f"<{key}>", lambda event: self.navigate(1))
        for key in ("Shift-Tab", "ISO_Left_Tab", "Up", "Left"):
            self.canvas.bind(f"<{key}>", lambda event: self.navigate(-1))

    def show(self):
        if self.visible:
            return
        self.visible = True
        hint = self.settings.get("monitor", self.desktop.cursor())
        area = self.desktop.work_area(hint)
        fraction = self.settings.get("vertical", 0.36)
        self.xy = self.dock(area, fraction)
        self.slide = 16.0 if self.side == "right" else -16.0
        self.window.deiconify()
        self.window.update_idletasks()
        self.handle = int(self.window.tk.call("wm", "frame", self.window._w), 16)
        self.place()
        self.draw()
        self.motion.to("alpha", (self.alpha,), (1.0,), 0.24, self.set_alpha)
        self.motion.to("slide", (self.slide,), (0.0,), 0.38, self.set_slide)

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.press = None
        self.dragged = False
        self.motion.cancel("slide")
        self.motion.cancel("position")
        self.expand(False)
        self.motion.to("alpha", (self.alpha,), (0.0,), 0.16, self.set_alpha, ease_in_out, self.window.withdraw)

    def destroy(self):
        self.motion.stop()
        self.window.destroy()

    def set_alpha(self, values):
        self.alpha = values[0]
        self.window.attributes("-alpha", self.alpha)

    def set_slide(self, values):
        self.slide = values[0]
        self.place()

    def place(self):
        self.desktop.move(self.handle, (self.xy[0] + self.slide, self.xy[1]))

    def set_position(self, values):
        self.xy = values
        self.place()

    def dock(self, area, fraction):
        x = area.left + GAP - PAD if self.side == "left" else area.right - GAP + PAD - WIDE
        top, bottom = area.top + GAP, area.bottom - GAP
        pill_top = top + max(0, bottom - top - SMALL_H) * fraction
        above, below = pill_top - top, bottom - pill_top - SMALL_H
        self.opens_up = below < TALL - 2 * PAD - SMALL_H and above > below
        if self.opens_up:
            y = max(top - PAD, pill_top + SMALL_H + PAD - TALL)
        else:
            y = min(pill_top - PAD, max(top - PAD, bottom + PAD - TALL))
        return x, y

    def set_content(self, project, detail, has_changes, available, task_label):
        if (project, detail, has_changes, available, task_label) != (
            self.project, self.detail, self.has_changes, self.available, self.task_label,
        ):
            self.project, self.detail, self.has_changes = project, detail, has_changes
            self.available, self.task_label = available, task_label
            self.draw()

    def set_page(self, frame=None, title="", back=None):
        if self.page is not None and self.page is not frame:
            self.page.place_forget()
        self.page, self.page_title, self.page_back = frame, title, back
        self.draw()

    def feedback(self):
        if not self.visible or not self.motion.enabled:
            return
        self.set_pulse((1.0,))
        self.motion.to("pulse", (1.0,), (0.0,), 0.72, self.set_pulse)

    def set_pulse(self, values):
        self.pulse = values[0]
        self.draw()

    def expand(self, expanded):
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self.focused = None
        target = (float(expanded),)
        self.motion.to("panel", (self.progress,), target, 0.38 if expanded else 0.24,
                       self.set_progress, ease_out if expanded else ease_in_out)
        self.motion.to("reveal", (self.reveal,), target, 0.49 if expanded else 0.15,
                       self.set_reveal, ease_in_out if expanded else ease_out)
        if not expanded:
            self.set_hover(None)

    def set_progress(self, values):
        self.progress = values[0]
        self.draw()

    def set_reveal(self, values):
        self.reveal = values[0]
        self.draw()

    def set_hover(self, action):
        if action is not None and action != self.hovered:
            self.action_hover = 0.0
            self.hovered = action
        target = (float(action is not None),)
        current = (self.action_hover,)
        pending = self.motion.tracks.get("action")
        if (pending and pending[3] == target) or (not pending and current == target):
            return
        self.motion.to("action", current, target, 0.16, self.paint_hover)

    def paint_hover(self, values):
        self.action_hover = values[0]
        self.draw()

    def bounds(self):
        width = mix(SMALL_W, WIDE - 2 * PAD, self.progress)
        height = mix(SMALL_H, TALL - 2 * PAD, self.progress)
        x = PAD if self.side == "left" else WIDE - PAD - width
        y = TALL - PAD - height if self.opens_up else PAD
        return x, y, width, height

    def clipped_text(self, face, text, maximum):
        if face.measure(text) <= maximum:
            return text
        while text and face.measure(text + "…") > maximum:
            text = text[:-1]
        return text + "…"

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        x, y, w, h = self.bounds()
        radius = mix(23, 18, self.progress)
        rounded(canvas, x, y + 2, w, h + 1, radius, "#0c1424")
        rounded(canvas, x, y, w, h, radius, blend(BORDER, "#96c9ff", self.pulse))
        rounded(canvas, x + 1, y + 1, w - 2, h - 2, radius - 1, PANEL)
        if self.page is not None:
            if self.expanded and self.progress >= 0.985:
                self.page.place(x=round(x + 13), y=round(y + 64),
                                width=round(w - 26), height=round(h - 77))
            else:
                self.page.place_forget()

        icon_x = x + 24
        icon_y = y + 24
        if self.page is None or self.progress < 0.84:
            canvas.create_oval(icon_x - 13, icon_y - 13, icon_x + 13, icon_y + 13,
                               fill=blend("#253e67", "#3c6aad", self.pulse), outline="")
            angle = 130 - 34 * ease_back(self.progress)
            canvas.create_arc(icon_x - 7, icon_y - 7, icon_x + 7, icon_y + 7, start=angle,
                              extent=292, style="arc", outline="#bbd0ff", width=1.8)
            tip = math.radians(angle)
            tx, ty = icon_x + 7 * math.cos(tip), icon_y - 7 * math.sin(tip)
            canvas.create_line(tx - 3, ty - 3, tx, ty, tx + 4, ty, fill="#bbd0ff", width=1.7)

        compact = max(0.0, 1 - self.progress * 3.4)
        if compact:
            canvas.create_text(x + 43, icon_y, text="菜单", anchor="w", font=self.action_font,
                               fill=blend(PANEL, WHITE, compact))
            if self.has_changes:
                canvas.create_oval(x + 86, icon_y - 2.5, x + 91, icon_y + 2.5,
                                   fill=blend(PANEL, ACCENT, compact), outline="")

        if self.progress < 0.84:
            return
        reveal = self.visibility(0.08)
        offset = 5 * (1 - reveal)
        title = self.page_title or "term-rewind"
        canvas.create_text(x + 44, y + 19 + offset, text=title, anchor="w", font=self.heading,
                           fill=blend(PANEL, WHITE, reveal))
        name = self.project or "未打开项目"
        line = f"{name}  ·  {self.detail}" if self.detail else name
        canvas.create_text(x + 44, y + 37 + offset,
                           text=self.clipped_text(self.small, line, w - 88), anchor="w", font=self.small,
                           fill=blend(PANEL, SUBTLE, reveal))
        canvas.create_line(x + 13, y + 55, x + w - 13, y + 55, fill=blend(PANEL, "#2c405e", reveal))
        for control in self.controls():
            self.draw_control(control, y + h)

    def visibility(self, delay):
        return ease_out(min(1.0, max(0.0, (self.reveal - delay) / 0.44)))

    def controls(self):
        x, y, width, _ = self.bounds()
        yield "collapse", "×", None, (x + width - 41, y + 12, 27, 26), 0.10
        if self.page is not None:
            yield "back", "‹", None, (x + 10, y + 12, 27, 26), 0.10
            return
        tile_width = (width - 34) / 2
        for index, (key, label, icon) in enumerate(TILES):
            row, column = divmod(index, 2)
            bounds = (x + 13 + column * (tile_width + 8), y + 67 + row * 51, tile_width, 43)
            yield key, self.task_label if key == "task" else label, icon, bounds, 0.13 + index * 0.037
        footer_width = (width - 26) / len(FOOTER)
        for index, (key, label) in enumerate(FOOTER):
            bounds = (x + 13 + footer_width * index, y + 286, footer_width - 3, 33)
            yield key, label, None, bounds, 0.47

    def allowed(self, key):
        return key in ("collapse", "back") or self.available.get(key, False)

    def draw_control(self, control, bottom):
        key, label, icon, (x, y, width, height), delay = control
        fade = self.visibility(delay)
        y += 7 * (1 - fade)
        if fade <= 0 or y + height > bottom - 5:
            return
        active = self.allowed(key)
        hover = self.action_hover if key == self.hovered and active else 0.0
        highlight = max(hover, 0.72 if key == self.focused and active else 0)
        fill = "#233551" if icon and active else "#1c2a40" if icon else PANEL
        if key == "restore" and active:
            fill = "#2c4673"
        fill = blend(fill, "#395a90", highlight)
        if key == self.focused and active:
            rounded(self.canvas, x - 1, y - 1, width + 2, height + 2, 10, blend(PANEL, ACCENT, fade))
        rounded(self.canvas, x, y, width, height, 9, blend(PANEL, fill, fade))
        foreground = WHITE if active else "#8799b5"
        if icon:
            draw_icon(self.canvas, icon, x + 17, y + height / 2,
                      blend(PANEL, ACCENT if active else "#8396b5", fade))
        self.canvas.create_text(
            x + 33 if icon else x + width / 2, y + height / 2,
            text=label, anchor="w" if icon else "center",
            font=self.heading if key in ("collapse", "back") else self.action_font,
            fill=blend(PANEL, foreground, fade),
        )

    def hit(self, x, y):
        left, top, width, height = self.bounds()
        if not (left <= x <= left + width and top <= y <= top + height):
            return None
        if self.expanded and self.progress > 0.84:
            for key, _, _, (cx, cy, cw, ch), delay in self.controls():
                if self.visibility(delay) > 0.42 and cx <= x <= cx + cw and cy <= y <= cy + ch:
                    return key
            if y > top + 55:
                return None
        return "panel"

    def pointer_moved(self, event):
        if not self.visible or self.press:
            return
        hit = self.hit(event.x, event.y)
        active = hit == "panel" or (hit and self.allowed(hit))
        self.canvas.configure(cursor="hand2" if active else "")
        self.set_hover(hit if hit and hit != "panel" and active else None)

    def activate(self, key):
        if key == "panel":
            self.expand(not self.expanded)
            if self.expanded:
                self.canvas.focus_force()
        elif key == "back" and self.page_back:
            self.page_back()
        elif self.allowed(key):
            if key not in ("collapse", "main", "disable", "quit"):
                self.invoke(key)
                return
            self.expand(False)
            if key != "collapse":
                self.invoke(key)

    def escape(self, event):
        if self.expanded and self.page_back:
            self.page_back()
        else:
            self.expand(False)
        return "break"

    def keyboard_activate(self, event):
        self.activate(self.focused or "panel")
        return "break"

    def navigate(self, step):
        if self.page is not None and self.expanded:
            return None
        if not self.expanded:
            self.activate("panel")
        keys = [key for key, *_ in self.controls() if self.allowed(key)]
        previous = keys.index(self.focused) if self.focused in keys else (-1 if step > 0 else 0)
        self.focused = keys[(previous + step) % len(keys)]
        self.draw()
        return "break"

    def press_at(self, event):
        target = self.hit(event.x, event.y)
        if target:
            left, top, width, height = self.bounds()
            offset = (event.x - left) / width, (event.y - top) / height
            self.press = (self.desktop.cursor(), target, offset)
            self.dragged = False

    def drag(self, event):
        if not self.press:
            return
        origin, target, offset = self.press
        if target != "panel":
            return
        x, y = self.desktop.cursor()
        dx, dy = x - origin[0], y - origin[1]
        if not self.dragged and abs(dx) + abs(dy) < 7:
            return
        if not self.dragged:
            self.dragged = True
            self.set_hover(None)
            self.motion.cancel("position")
            self.motion.cancel("slide")
            self.slide = 0.0
            self.motion.cancel("panel")
            self.motion.cancel("reveal")
            self.expanded = False
            self.progress = self.reveal = 0.0
            self.draw()
        left, top, width, height = self.bounds()
        self.set_position((x - left - offset[0] * width, y - top - offset[1] * height))

    def release(self, event):
        if not self.press:
            return
        _, target, _ = self.press
        self.press = None
        if not self.dragged:
            if self.hit(event.x, event.y) == target:
                self.activate(target)
            return
        self.dragged = False
        cursor = self.desktop.cursor()
        area = self.desktop.work_area(cursor)
        left, top, _, _ = self.bounds()
        compact_x, compact_y = self.xy[0] + left, self.xy[1] + top
        self.side = "left" if cursor[0] < (area.left + area.right) / 2 else "right"
        available = max(1, area.bottom - area.top - SMALL_H - 2 * GAP)
        vertical = min(1.0, max(0.0, (compact_y - area.top - GAP) / available))
        destination = self.dock(area, vertical)
        left, top, _, _ = self.bounds()
        self.xy = (compact_x - left, compact_y - top)
        self.place()
        self.draw()
        self.motion.to("position", self.xy, destination, 0.43, self.set_position, ease_out)
        self.settings.update(side=self.side, vertical=vertical, monitor=list(cursor))
        self.save_position()

    def context_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
