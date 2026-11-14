"""Cross-platform keyboard timeline for consoles with virtual terminal support."""

from contextlib import contextmanager
import os
import shutil
import sys

from rewind import RewindError, change_type, file_diff, safe_text, summary

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt
else:
    import select
    import termios
    import tty


ACCENT = "\x1b[96m"
DIM = "\x1b[90m"
ADDED = "\x1b[32m"
REMOVED = "\x1b[31m"
META = "\x1b[36m"
RESET = "\x1b[0m"
SELECTED = "\x1b[30;106m"


@contextmanager
def virtual_terminal():
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(-11)
        previous = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(previous)):
            raise RewindError("An interactive console is required. Use rewind list here.")
        if not kernel.SetConsoleMode(handle, previous.value | 0x0004):
            raise RewindError("This console cannot render the timeline. Use rewind list here.")
        restore = lambda: kernel.SetConsoleMode(handle, previous.value)
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RewindError("An interactive terminal is required. Use rewind list here.")
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        tty.setraw(fd)
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()
    try:
        yield
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        restore()


def _read_posix_escape():
    fd = sys.stdin.fileno()
    sequence = ""
    for _ in range(4):
        ready, _, _ = select.select([fd], [], [], 0.03)
        if not ready:
            break
        char = sys.stdin.read(1)
        if not char:
            break
        sequence += char
        if char.isalpha() or char == "~":
            break
    if not sequence:
        return "esc"
    return {
        "[A": "up", "OA": "up",
        "[B": "down", "OB": "down",
        "[5~": "pgup",
        "[6~": "pgdn",
    }.get(sequence)


def read_key():
    if os.name == "nt":
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}.get(msvcrt.getwch())
        return {"\r": "enter", "\x1b": "esc"}.get(key, key.lower())
    key = sys.stdin.read(1)
    if key == "\x1b":
        return _read_posix_escape()
    if key in ("\r", "\n"):
        return "enter"
    return key.lower()


def _colorize_diff(raw, line):
    if raw.startswith(("+++", "---", "@@")):
        return META + line + RESET
    if raw.startswith("+"):
        return ADDED + line + RESET
    if raw.startswith("-"):
        return REMOVED + line + RESET
    return line


def draw(subtitle, lines, footer, cursor=None, colored=False):
    width, height = shutil.get_terminal_size((96, 27))
    width = max(1, width - 1)
    page = max(1, height - 7)
    offset = max(0, (cursor or 0) - page + 1)
    screen = ["", ACCENT + "  TERM REWIND"[:width] + RESET, "  " + subtitle[:max(0, width - 2)], ""]
    for index in range(offset, min(len(lines), offset + page)):
        raw = lines[index]
        line = ("  " + raw)[:width]
        if cursor == index:
            line = SELECTED + line.ljust(width) + RESET
        elif colored:
            line = _colorize_diff(raw, line)
        screen.append(line)
    screen.extend(["", DIM + ("  " + footer)[:width] + RESET])
    sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(screen))
    sys.stdout.flush()
    return page


def pick(title, lines, footer):
    cursor = 0
    while True:
        page = draw(title, lines, footer, cursor if lines else None)
        key = read_key()
        if key in ("esc", "q", "\x03"):
            return None
        if lines and key in ("enter", "u", "a"):
            return key, cursor
        movement = {"up": -1, "k": -1, "down": 1, "j": 1, "pgup": -page, "pgdn": page}.get(key, 0)
        cursor = min(max(0, len(lines) - 1), max(0, cursor + movement))


def read_lines(title, lines, allow_undo=False, colored=False):
    offset = 0
    while True:
        footer = "Up/Down (j/k)  PgUp/PgDn  Esc Back"
        if allow_undo:
            footer += "  U Undo this file"
        page = draw(title, lines[offset:], footer, colored=colored)
        key = read_key()
        if key in ("esc", "q", "enter", "\x03"):
            return False
        if key == "u" and allow_undo:
            return True
        movement = {"up": -1, "k": -1, "down": 1, "j": 1, "pgup": -page, "pgdn": page}.get(key, 0)
        offset = min(max(0, len(lines) - page), max(0, offset + movement))


def apply(project, record, paths):
    draw(
        f"Undo {len(paths)} file change(s) from {record['id']}?",
        list(map(safe_text, paths)),
        "Y Confirm  Any other key Cancel",
    )
    if read_key() != "y":
        return
    try:
        with project.lock():
            restored = project.undo(record, paths)
        read_lines("Undo recorded", [summary(restored)])
    except (RewindError, OSError) as error:
        message = [safe_text(line) for line in str(error).splitlines()]
        if "Conflicts" in str(error):
            draw("Undo stopped (conflict)", message, "F Force overwrite  Any other key Back")
            if read_key() == "f":
                try:
                    with project.lock():
                        restored = project.undo(record, paths, force=True)
                    read_lines("Undo recorded (forced)", [summary(restored)])
                except (RewindError, OSError) as force_error:
                    read_lines("Undo stopped", [safe_text(line) for line in str(force_error).splitlines()])
            return
        read_lines("Undo stopped", message)


def record_view(project, record):
    changes = record.get("changes", {})
    paths = list(changes)
    skipped = record.get("skipped", {})
    lines = [f"{change_type(*changes[path])}  {safe_text(path)}" for path in paths]
    lines += [f"?  {safe_text(path)}  ({reason})" for path, reason in skipped.items()]
    if not lines:
        message = "No file changes in this record."
        if record["state"] == "pending":
            message = f"Open record. Close it with: rewind end {record['id']}"
        read_lines(summary(record), [message])
        return
    while choice := pick(summary(record), lines, "Up/Down  Enter Diff  U Undo file  A Undo all  Esc Back"):
        action, index = choice
        if action == "a" and paths:
            apply(project, record, paths)
        elif index < len(paths):
            path = paths[index]
            if action == "u":
                apply(project, record, [path])
            elif action == "enter":
                try:
                    preview = file_diff(project, path, *changes[path])
                except (OSError, RewindError) as error:
                    read_lines("Preview unavailable", [safe_text(error)])
                    continue
                if read_lines("Before -> After", preview, allow_undo=True, colored=True):
                    apply(project, record, [path])


def show(project, records):
    with virtual_terminal():
        while True:
            if not records:
                read_lines("Recent commands", ["No history. Start with rewind run -- <program>."])
                return
            choice = pick("Recent commands and tasks", list(map(summary, records)), "Up/Down  Enter Open  Esc Quit")
            if choice is None:
                return
            if choice[0] == "enter":
                record_view(project, records[choice[1]])
                records = project.records()[:30]
