"""Local, command-scoped file snapshots for term-rewind."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid


__version__ = "0.1.0"
MIB = 1024 * 1024
FILE_LIMIT = 8 * MIB
PROJECT_LIMIT = 128 * MIB
EXCLUDED = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".rewind",
    "target", ".next", ".nuxt", ".turbo", ".svelte-kit",
    ".parcel-cache", ".gradle", ".tox", ".nox",
}
RACY_WINDOW_NS = 50_000_000


class RewindError(Exception):
    pass


def safe_text(value):
    return "".join(
        char if unicodedata.category(char)[0] != "C" else ascii(char)[1:-1]
        for char in str(value)
    )


def digest(data):
    return hashlib.sha256(data).hexdigest()


def win_path(path):
    path = Path(path)
    if os.name != "nt":
        return path
    raw = os.path.abspath(str(path))
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def strip_win_prefix(path):
    text = str(path)
    if os.name == "nt":
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
    return Path(text)


def atomic_write(path, data):
    target = win_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".rewind-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        win_path(name).unlink(missing_ok=True)


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def is_valid_key(key):
    return isinstance(key, str) and len(key) == 64 and all(char in "0123456789abcdef" for char in key)


def excluded(name, relative="", patterns=()):
    lowered = name.casefold()
    if lowered in EXCLUDED or lowered == ".env" or lowered.startswith(".env."):
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(lowered, pattern) or (relative and fnmatch.fnmatch(relative.casefold(), pattern)):
            return True
    return False


def read_regular(path):
    target = win_path(path)
    initial = target.lstat()
    if is_link(initial) or not stat.S_ISREG(initial.st_mode):
        raise RewindError(f"Not a regular file: {path}")
    if initial.st_size > FILE_LIMIT:
        raise RewindError(f"File exceeds 8 MiB: {path}")
    with target.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        data = handle.read(FILE_LIMIT + 1)
        final = os.fstat(handle.fileno())
    current = target.lstat()
    signatures = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (initial, opened, final, current)
    }
    if (
        len(signatures) != 1 or is_link(current) or len(data) != current.st_size
        or initial.st_ctime_ns != current.st_ctime_ns or opened.st_ctime_ns != final.st_ctime_ns
    ):
        raise RewindError(f"File changed during capture: {path}")
    return data


@contextmanager
def exclusive(path):
    target = win_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RewindError("Another rewind operation is active for this project.") from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RewindError("Another rewind operation is active for this project.") from error
        yield


def skipped_path(path, skipped):
    parts = path.split("/")
    return any("/".join(parts[:end]) in skipped for end in range(1, len(parts) + 1))


class Project:
    def __init__(self, root, storage):
        resolved_root = win_path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise RewindError("The project root must be a directory.")
        self.root = strip_win_prefix(resolved_root)
        storage = strip_win_prefix(win_path(storage).resolve())
        if storage.is_relative_to(self.root) or self.root.is_relative_to(storage):
            raise RewindError("Keep the snapshot store separate from the project directory.")
        full_key = digest(os.path.normcase(str(self.root)).encode("utf-8", "surrogatepass"))
        legacy_store = storage / full_key
        short_store = storage / full_key[:16]
        self.store = legacy_store if win_path(legacy_store).exists() else short_store

    def lock(self):
        return exclusive(self.store / "project.lock")

    def object_path(self, key):
        return win_path(self.store / "objects" / key[:2] / key[2:])

    def object_data(self, key):
        if key is None:
            return b""
        if not is_valid_key(key):
            raise RewindError("Invalid snapshot object identifier.")
        data = self.object_path(key).read_bytes()
        if digest(data) != key:
            raise RewindError("A snapshot object failed its integrity check.")
        return data

    def ignore_patterns(self):
        ignore_file = win_path(self.root / ".rewindignore")
        try:
            info = ignore_file.lstat()
        except FileNotFoundError:
            return ()
        if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return ()
        patterns = []
        for raw in ignore_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.replace("\\", "/").lstrip("./").rstrip("/")
            if line:
                patterns.append(line.casefold())
        return tuple(patterns)

    def load_stat_cache(self):
        path = win_path(self.store / "stat-cache.json")
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_stat_cache(self, cache):
        atomic_write(self.store / "stat-cache.json", json.dumps(cache, ensure_ascii=True).encode("utf-8"))

    def capture(self):
        files, skipped = {}, {}
        total = 0
        patterns = self.ignore_patterns()
        cache = self.load_stat_cache()
        new_cache = {}
        capture_started = time.time_ns()
        scan_root = win_path(self.root)

        def cached_key(relative, info):
            entry = cache.get(relative)
            if not (isinstance(entry, list) and len(entry) == 7):
                return None
            dev, ino, mtime, ctime, size, key, cached_at = entry
            if (dev, ino, mtime, ctime, size) != (
                info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size
            ):
                return None
            if not isinstance(cached_at, int) or info.st_mtime_ns >= cached_at - RACY_WINDOW_NS:
                return None
            if info.st_ctime_ns >= cached_at - RACY_WINDOW_NS:
                return None
            if not is_valid_key(key) or not self.object_path(key).is_file():
                return None
            return key, entry

        def scan(directory):
            nonlocal total
            for path in sorted(directory.iterdir()):
                relative = path.relative_to(scan_root).as_posix()
                if excluded(path.name, relative, patterns):
                    skipped[relative] = "excluded"
                    continue
                info = path.lstat()
                if is_link(info):
                    skipped[relative] = "link or reparse point"
                elif stat.S_ISDIR(info.st_mode):
                    scan(path)
                elif not stat.S_ISREG(info.st_mode):
                    skipped[relative] = "not a regular file"
                elif info.st_size > FILE_LIMIT:
                    skipped[relative] = "over 8 MiB"
                else:
                    total += info.st_size
                    if total > PROJECT_LIMIT or len(files) >= 5000:
                        raise RewindError("Snapshot exceeds 128 MiB or 5,000 files. Use a smaller project root.")
                    hit = cached_key(relative, info)
                    if hit is not None:
                        key, entry = hit
                        new_cache[relative] = entry
                    else:
                        data = read_regular(path)
                        key = digest(data)
                        target = self.object_path(key)
                        if not target.exists():
                            atomic_write(target, data)
                        new_cache[relative] = [
                            info.st_dev, info.st_ino, info.st_mtime_ns,
                            info.st_ctime_ns, info.st_size, key, capture_started,
                        ]
                    files[relative] = key

        scan(scan_root)
        self.save_stat_cache(new_cache)
        return {"files": files, "skipped": skipped}

    def save(self, record):
        destination = self.store / "history" / (record["id"] + ".json")
        atomic_write(destination, json.dumps(record, ensure_ascii=True, indent=2).encode("utf-8"))

    def records(self):
        history = win_path(self.store / "history")
        if not history.exists():
            return []
        result = []
        for path in history.glob("*.json"):
            try:
                data = json.loads(path.read_bytes())
                if not isinstance(data, dict) or "id" not in data or "order" not in data or "state" not in data:
                    raise ValueError("Invalid record structure")
                result.append(data)
            except (OSError, ValueError) as error:
                info = path.stat()
                result.append({
                    "id": path.stem,
                    "order": info.st_mtime_ns,
                    "created": datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="seconds"),
                    "kind": "corrupt",
                    "label": f"Corrupt history file: {safe_text(path.name)}",
                    "state": "corrupt",
                    "changes": {},
                    "skipped": {},
                    "error": safe_text(error),
                })
        return sorted(result, key=lambda record: record["order"], reverse=True)

    def get(self, identifier):
        records = self.records()
        for record in records:
            if record["id"] == identifier:
                return record
        matches = [record for record in records if identifier and record["id"].startswith(identifier)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RewindError(f"Ambiguous record prefix: {identifier}")
        raise RewindError(f"Unknown record: {identifier}")

    def latest_complete(self):
        for record in self.records():
            if record.get("state") in ("complete", "partial"):
                return record
        raise RewindError("No completed command or task found.")

    def record_failure(self, label, kind, error, agent=None, session=None):
        record = new_record(label, kind, agent=agent, session=session)
        record.update(state="failed", changes={}, skipped={}, exit_code=None, error=safe_text(error))
        self.save(record)
        return record

    def latest_pending(self, session=None, call_id=None):
        records = self.records()
        if call_id is not None:
            for record in records:
                if record["state"] == "pending" and record.get("call_id") == call_id:
                    return record
            raise RewindError(f"No open command or task found for call_id: {call_id}")
        for record in records:
            if record["state"] == "pending" and (session is None or record.get("session") == session):
                return record
        raise RewindError("No open command or task found.")

    def start(self, label, kind, agent=None, session=None, step=None, baseline=None, call_id=None):
        if call_id is not None:
            for existing in self.records():
                if existing["state"] == "pending" and existing.get("call_id") == call_id:
                    raise RewindError(f"A pending record already exists for call_id: {call_id}")
        baseline = self.capture() if baseline is None else baseline
        siblings = [
            rec for rec in self.records()
            if rec.get("state") == "pending" and "baseline" in rec
        ]
        batch = None
        concurrent = False
        if siblings:
            batch = siblings[0].get("batch") or siblings[0]["id"]
            concurrent = True
            for sibling in siblings:
                if sibling.get("batch") != batch or not sibling.get("concurrent"):
                    sibling["batch"] = batch
                    sibling["concurrent"] = True
                    self.save(sibling)
        record = new_record(
            label, kind, agent=agent, session=session, step=step,
            call_id=call_id, batch=batch, concurrent=concurrent,
        )
        record["baseline"] = baseline
        self.save(record)
        return record

    def finish(self, record, exit_code=None, captured=None, cancelled=False, error=None):
        if record["state"] != "pending" or "baseline" not in record:
            raise RewindError("Only an open command or task can be ended.")
        after = self.capture() if captured is None else captured
        before = record.pop("baseline")
        skipped = before["skipped"] | after["skipped"]
        previous, current = before["files"], after["files"]
        changes = {
            path: [previous.get(path), current.get(path)]
            for path in sorted(previous.keys() | current.keys())
            if previous.get(path) != current.get(path) and not skipped_path(path, skipped)
        }
        record.update(state="complete", changes=changes, skipped=skipped, exit_code=exit_code)
        if cancelled:
            record["cancelled"] = True
        if error:
            record["error"] = safe_text(error)
        self.save(record)
        return record

    def step(self, label, agent=None, session=None):
        current = self.capture()
        prev = None
        for record in self.records():
            if record["state"] == "pending" and "baseline" in record:
                if session is None or record.get("session") == session:
                    prev = record
                    break
        closed = None
        if prev is not None:
            agent = agent or prev.get("agent")
            session = session or prev.get("session") or prev["id"]
            if "session" not in prev:
                prev["session"] = session
            if "step" not in prev:
                prev["step"] = 1
            closed = self.finish(prev, captured=current)
            next_step = prev["step"] + 1
        else:
            next_step = 1
        opened = self.start(label, "task", agent=agent, session=session, step=next_step, baseline=current)
        return closed, opened

    def target(self, relative):
        parts = relative.split("/")
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            raise RewindError("Invalid path in the record.")
        current = win_path(self.root)
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if is_link(info) or current.resolve(strict=True) != current:
                raise RewindError("Path now passes through a link or alias.")
            if os.name == "nt" and current.resolve(strict=True).name != current.name:
                raise RewindError("Path casing changed.")
        return current

    def current_key(self, path):
        target = self.target(path)
        try:
            return digest(read_regular(target))
        except FileNotFoundError:
            return None

    def preview_undo(self, record, selected, force=False):
        changes = get_changes(record)
        planned = {path: [changes[path][1], changes[path][0]] for path in selected}
        if not planned:
            raise RewindError("This record has no file changes.")
        conflicts = []
        resolved = {}
        to_archive = {}
        for path, (expected, previous) in planned.items():
            try:
                target = self.target(path)
                try:
                    current_data = read_regular(target)
                    actual = digest(current_data)
                except FileNotFoundError:
                    current_data = None
                    actual = None
                if actual != expected:
                    conflicts.append(f"{safe_text(path)}: current content differs")
                self.object_data(previous)
                resolved[path] = [actual, previous]
                if force and current_data is not None and not self.object_path(actual).exists():
                    to_archive[actual] = current_data
            except (OSError, RewindError) as error:
                conflicts.append(f"{safe_text(path)}: {safe_text(error)}")
        return {
            "paths": list(planned),
            "conflicts": conflicts,
            "resolved": resolved,
            "to_archive": to_archive,
        }

    def undo(self, record, selected, force=False):
        plan = self.preview_undo(record, selected, force=force)
        conflicts = plan["conflicts"]
        if conflicts and not force:
            raise RewindError(
                "Undo stopped before writing. Conflicts (re-run with --force to overwrite):\n  "
                + "\n  ".join(conflicts)
            )
        if conflicts and force:
            hard_errors = [c for c in conflicts if not c.endswith(": current content differs")]
            if hard_errors:
                raise RewindError("Undo stopped before writing:\n  " + "\n  ".join(hard_errors))
        resolved = plan["resolved"]
        for key, data in plan["to_archive"].items():
            atomic_write(self.object_path(key), data)

        undo_record = new_record(f"Undo {record['id']} - {record['label']}", "undo")
        undo_record["planned"] = resolved
        self.save(undo_record)
        applied = {}
        try:
            for path, (expected, previous) in resolved.items():
                if self.current_key(path) != expected:
                    raise RewindError(f"Current content changed: {safe_text(path)}")
                target = self.target(path)
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, self.object_data(previous))
                applied[path] = [expected, previous]
        except (OSError, RewindError, KeyboardInterrupt) as error:
            self.finish_undo(undo_record, applied, "partial")
            raise RewindError(
                f"Undo stopped after {len(applied)} file(s). Record {undo_record['id']}: {error}"
            ) from error
        return self.finish_undo(undo_record, applied, "complete")

    def finish_undo(self, record, applied, state):
        del record["planned"]
        record.update(state=state, changes=applied, skipped={}, exit_code=None)
        self.save(record)
        return record

    def recover_undo(self, record):
        applied = {}
        for path, (expected, previous) in record["planned"].items():
            if self.current_key(path) == previous:
                applied[path] = [expected, previous]
        return self.finish_undo(record, applied, "partial")

    def gc(self, keep=20):
        if keep < 0:
            raise RewindError("Keep count must be zero or greater.")
        records = self.records()
        kept, removed = [], []
        completed_seen = 0
        for record in records:
            if record.get("state") == "pending":
                kept.append(record)
            elif completed_seen < keep:
                kept.append(record)
                completed_seen += 1
            else:
                removed.append(record)
        history = win_path(self.store / "history")
        for record in removed:
            (history / (record["id"] + ".json")).unlink(missing_ok=True)
        referenced = set()
        for record in kept:
            referenced.update(record.get("baseline", {}).get("files", {}).values())
            for pair in record.get("changes", {}).values():
                referenced.update(pair)
            for pair in record.get("planned", {}).values():
                referenced.update(pair)
        referenced.discard(None)
        objects_dir = win_path(self.store / "objects")
        removed_objects = 0
        freed_bytes = 0
        if objects_dir.exists():
            for prefix in list(objects_dir.iterdir()):
                if not prefix.is_dir():
                    continue
                for obj in list(prefix.iterdir()):
                    if obj.is_file() and (prefix.name + obj.name) not in referenced:
                        freed_bytes += obj.stat().st_size
                        obj.unlink()
                        removed_objects += 1
                if not any(prefix.iterdir()):
                    prefix.rmdir()
        cache = self.load_stat_cache()
        if cache:
            pruned_cache = {
                path: entry for path, entry in cache.items()
                if isinstance(entry, list) and len(entry) == 7 and self.object_path(entry[5]).is_file()
            }
            if len(pruned_cache) != len(cache):
                self.save_stat_cache(pruned_cache)
        return len(removed), removed_objects, freed_bytes

    def stats(self):
        records = self.records()
        objects_dir = win_path(self.store / "objects")
        object_count = 0
        total_bytes = 0
        if objects_dir.exists():
            for prefix in objects_dir.iterdir():
                if not prefix.is_dir():
                    continue
                for obj in prefix.iterdir():
                    if obj.is_file():
                        object_count += 1
                        total_bytes += obj.stat().st_size
        return {
            "root": str(self.root),
            "store": str(self.store),
            "records": len(records),
            "pending": sum(rec.get("state") == "pending" for rec in records),
            "failed": sum(rec.get("state") == "failed" for rec in records),
            "corrupt": sum(rec.get("state") == "corrupt" for rec in records),
            "objects": object_count,
            "bytes": total_bytes,
        }


def new_record(label, kind, agent=None, session=None, step=None, call_id=None, batch=None, concurrent=False):
    created = datetime.now().astimezone()
    record = {
        "id": uuid.uuid4().hex[:12], "order": time.time_ns(),
        "created": created.isoformat(timespec="seconds"), "kind": kind, "label": label, "state": "pending",
    }
    if agent:
        record["agent"] = safe_text(agent)
    if session:
        record["session"] = safe_text(session)
    if step is not None:
        record["step"] = int(step)
    if call_id:
        record["call_id"] = safe_text(call_id)
    if batch:
        record["batch"] = safe_text(batch)
    if concurrent:
        record["concurrent"] = True
    return record


def get_changes(record):
    if record["state"] == "pending":
        raise RewindError(f"Record {record['id']} is open. Close it with: rewind end {record['id']}")
    if record["state"] in ("failed", "corrupt"):
        detail = record.get("error") or record["state"]
        raise RewindError(f"Record {record['id']} was not captured ({detail}).")
    return record["changes"]


def counts(record):
    changes = record.get("changes", {})
    added = sum(old is None for old, new in changes.values())
    deleted = sum(new is None for old, new in changes.values())
    return f"+{added}  ~{len(changes) - added - deleted}  -{deleted}"


def summary(record):
    status = record["state"]
    if status == "complete":
        status = f"exit {record['exit_code']}" if record["exit_code"] is not None else "done"
    if record.get("cancelled"):
        status = "cancelled"
    tags = []
    if record.get("agent"):
        step_tag = f"#{record['step']}" if record.get("step") is not None else ""
        tags.append(f"{safe_text(record['agent'])}{step_tag}")
    elif record.get("step") is not None:
        tags.append(f"#{record['step']}")
    if record.get("concurrent"):
        tags.append("batch")
    actor = f"({', '.join(tags)})  " if tags else ""
    return (
        f"{record['id']}  {record['created'][5:19].replace('T', ' ')}  "
        f"{counts(record)}  [{status}]  {actor}{safe_text(record['label'])}"
    )


def select_paths(changes, names):
    if names is None:
        return list(changes)
    if len(names) == 0:
        raise RewindError("No files selected. Specify file path(s) or omit to select all.")
    selected = []
    for name in names:
        path = name.replace("\\", "/").removeprefix("./")
        if path not in changes:
            raise RewindError(f"Path is not in this record: {safe_text(name)}")
        if path not in selected:
            selected.append(path)
    return selected


def change_type(before, after):
    return "A" if before is None else "D" if after is None else "M"


def file_diff(project, path, before, after):
    status = change_type(before, after)
    heading = f"{status}  {safe_text(path)}"
    old, new = project.object_data(before), project.object_data(after)
    if max(len(old), len(new)) > 65536:
        return [heading, f"Text preview capped at 64 KiB ({len(old)} -> {len(new)} bytes)."]
    try:
        if b"\x00" in old or b"\x00" in new:
            raise ValueError
        old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
    except ValueError:
        return [heading, f"Binary or non-UTF-8 file ({len(old)} -> {len(new)} bytes)."]
    old_lines, new_lines = old_text.splitlines(keepends=True), new_text.splitlines(keepends=True)
    if max(len(old_lines), len(new_lines)) > 2000:
        return [heading, "Text preview capped at 2,000 lines."]
    before_label = f"a/{safe_text(path)}" if before else "/dev/null"
    after_label = f"b/{safe_text(path)}" if after else "/dev/null"
    diff = difflib.unified_diff(old_lines, new_lines, before_label, after_label)
    lines = []
    for line in diff:
        lines.append(safe_text(line.rstrip("\r\n")))
        if not line.endswith(("\r", "\n")):
            lines.append("\\ No newline at end of file")
    return [heading] + (lines or ["(Empty file)"])


def default_storage():
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_STATE_HOME")
    return Path(base or (Path.home() / ".local" / "state")) / "term-rewind"


def prepare_command(project, command):
    if not command:
        raise RewindError("Supply an external program after: rewind run --")
    program = command[0]
    if os.path.dirname(program) and not os.path.isabs(program):
        program = str(project.root / program)
    executable = shutil.which(program)
    if executable is None:
        raise RewindError(f"Program not found: {safe_text(command[0])}")
    if os.name == "nt" and Path(executable).suffix.casefold() not in {".exe", ".com"}:
        raise RewindError("Use an executable, e.g. pwsh -File script.ps1 or node script.js.")
    return [executable, *command[1:]]


def run_command(project, command):
    if command and command[0] == "--":
        command = command[1:]
    program = prepare_command(project, command)
    label = subprocess.list2cmdline(command)
    with project.lock():
        record = project.start(label, "command")
        print(f"Recording {record['id']} - {safe_text(label)}", flush=True)
        code = 1
        try:
            code = subprocess.call(program, cwd=project.root)
        except KeyboardInterrupt:
            code = 130
        finally:
            try:
                project.finish(record, code)
                print(summary(record))
                if record["skipped"]:
                    print(f"{len(record['skipped'])} skipped path(s); see: rewind diff {record['id']}")
            except (OSError, RewindError) as error:
                raise RewindError(
                    f"Capture incomplete. Baseline retained as {record['id']}; use rewind end to retry. {error}"
                ) from error
        return code if code >= 0 else 128 - code


HOOKS = {
    "zsh": """\
# Add to ~/.zshrc, then run `export TERM_REWIND_AUTO=1` in a project directory.
_rewind_preexec() {
  [[ "${TERM_REWIND_AUTO:-0}" == "1" ]] || return 0
  [[ "$1" == rewind* || "$1" == "term-rewind"* ]] && return 0
  _REWIND_ACTIVE_ID="$(rewind begin "$1" --warn-only)"
}
_rewind_precmd() {
  local code=$?
  if [[ -n "$_REWIND_ACTIVE_ID" ]]; then
    rewind end "$_REWIND_ACTIVE_ID" --exit-code "$code" --warn-only >/dev/null
    unset _REWIND_ACTIVE_ID
  fi
}
autoload -Uz add-zsh-hook
add-zsh-hook preexec _rewind_preexec
add-zsh-hook precmd _rewind_precmd""",
    "bash": """\
# Add to ~/.bashrc, then run `export TERM_REWIND_AUTO=1` in a project directory.
_rewind_preexec() {
  [[ "${TERM_REWIND_AUTO:-0}" == "1" ]] || return 0
  [[ -n "$_REWIND_IN_PROMPT" ]] && return 0
  [[ "$BASH_COMMAND" == _rewind_* || "$BASH_COMMAND" == rewind* || "$BASH_COMMAND" == "term-rewind"* ]] && return 0
  [[ -z "$_REWIND_ACTIVE_ID" ]] && _REWIND_ACTIVE_ID="$(rewind begin "$BASH_COMMAND" --warn-only)"
}
_rewind_precmd() {
  local code=$?
  _REWIND_IN_PROMPT=1
  if [[ -n "$_REWIND_ACTIVE_ID" ]]; then
    rewind end "$_REWIND_ACTIVE_ID" --exit-code "$code" --warn-only >/dev/null
    unset _REWIND_ACTIVE_ID
  fi
  unset _REWIND_IN_PROMPT
}
trap '_rewind_preexec' DEBUG
PROMPT_COMMAND="_rewind_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}" """,
    "powershell": """\
# Add to $PROFILE (`notepad $PROFILE`) for a short command wrapper.
function rw {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Command)
    if (-not $Command) { rewind; return }
    rewind run -- @Command
}""",
    "claude": """\
// Add to .claude/settings.json (warn mode: pairs parallel calls via stdin tool_use_id and covers failures).
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind begin \\"claude: tool\\" --agent claude --claude-hook --warn-only >/dev/null" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind end --claude-hook --warn-only >/dev/null" }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind end --claude-hook --exit-code 1 --warn-only >/dev/null" }
        ]
      }
    ]
  }
}""",
    "claude-strict": """\
// Add to .claude/settings.json (strict mode: blocks tool execution with exit code 2 if pre-snapshot fails).
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind begin \\"claude: tool\\" --agent claude --claude-hook --strict >/dev/null" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind end --claude-hook --warn-only >/dev/null" }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          { "type": "command", "command": "rewind end --claude-hook --exit-code 1 --warn-only >/dev/null" }
        ]
      }
    ]
  }
}""",
    "mcp": """\
// Add to your MCP client config (Claude Code, Cursor, Antigravity, etc.)
{
  "mcpServers": {
    "term-rewind": {
      "command": "rewind",
      "args": ["mcp", "--root", "."]
    }
  }
}""",
}


def read_claude_hook_context():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cli():
    parser = argparse.ArgumentParser(prog="rewind", description=__doc__)
    parser.add_argument("-V", "--version", action="version", version=f"term-rewind {__version__}")
    parser.add_argument("--root", default=".", help="Project directory, defaults to the current directory.")
    parser.add_argument("--store", default=default_storage(), help="Snapshot directory outside the project.")
    commands = parser.add_subparsers(dest="action")
    run = commands.add_parser("run", help="Record one external program.")
    run.add_argument("command", nargs=argparse.REMAINDER)
    list_cmd = commands.add_parser("list", help="Show recent records.")
    list_cmd.add_argument("--agent", default=None, help="Filter records by agent name.")
    list_cmd.add_argument("--session", default=None, help="Filter records by session ID.")
    list_cmd.add_argument("--limit", type=int, default=10, help="Max records to display (default: 10).")
    list_cmd.add_argument("--json", action="store_true", help="Emit JSON output.")
    commands.add_parser("ui", help="Open the timeline.")
    status_cmd = commands.add_parser("status", help="Show snapshot storage usage and record counts.")
    status_cmd.add_argument("--json", action="store_true", help="Emit JSON output.")
    begin = commands.add_parser("begin", help="Take a baseline before a task.")
    begin.add_argument("label")
    begin.add_argument("--agent", default=None, help="Attribute this task to an agent (or TERM_REWIND_AGENT).")
    begin.add_argument("--session", default=None, help="Optional session/task group ID (or TERM_REWIND_SESSION).")
    begin.add_argument("--call-id", default=None, help="Optional tool call ID to pair begin and end for concurrent calls.")
    begin.add_argument("--claude-hook", action="store_true", help="Read Claude Code hook JSON from stdin for call_id/session.")
    begin.add_argument("--warn-only", action="store_true", help="Warn on stderr and record a failed entry if capture fails.")
    begin.add_argument("--strict", action="store_true", help="Exit with code 2 if baseline capture fails (blocks agent hooks).")
    step_cmd = commands.add_parser("step", help="Close the current pending step and open the next one in one scan.")
    step_cmd.add_argument("label")
    step_cmd.add_argument("--agent", default=None, help="Attribute this step to an agent.")
    step_cmd.add_argument("--session", default=None, help="Session ID to continue.")
    step_cmd.add_argument("--warn-only", action="store_true", help="Warn on stderr and record a failed entry if capture fails.")
    step_cmd.add_argument("--strict", action="store_true", help="Exit with code 2 if capture fails.")
    end = commands.add_parser("end", help="Close a task or reconcile an interrupted undo.")
    end.add_argument("id", nargs="?", help="Record ID or prefix; defaults to the latest pending record.")
    end.add_argument("--session", default=None, help="Close latest pending record in this session.")
    end.add_argument("--call-id", default=None, help="Close the pending record matching this tool call ID.")
    end.add_argument("--claude-hook", action="store_true", help="Read Claude Code hook JSON from stdin for call_id/failure state.")
    end.add_argument("--exit-code", type=int, default=None, help="Optional exit code to record.")
    end.add_argument("--warn-only", action="store_true", help="Warn on stderr without failing if closing fails.")
    end.add_argument("--strict", action="store_true", help="Exit with code 2 if closing fails.")
    for action in ("diff", "undo"):
        command = commands.add_parser(action)
        command.add_argument("id", nargs="?", help="Record ID or prefix; defaults to the latest completed record.")
        command.add_argument("--file", action="append", default=[], help="Select a recorded path. Repeatable.")
        if action == "diff":
            command.add_argument("--json", action="store_true", help="Emit JSON output.")
        if action == "undo":
            command.add_argument("--yes", action="store_true", help="Apply without the interactive prompt.")
            command.add_argument("--force", action="store_true", help="Overwrite even if files changed afterwards.")
    gc = commands.add_parser("gc", help="Prune old history records and unreferenced snapshot objects.")
    gc.add_argument("--keep", type=int, default=20, help="Number of recent completed records to keep (default: 20).")
    hook = commands.add_parser("hook", help="Print a shell or AI agent integration snippet.")
    hook.add_argument("target", choices=sorted(HOOKS), help="Integration target.")
    hook.add_argument("--strict", action="store_true", help="Print strict blocking variant when available.")
    mcp = commands.add_parser("mcp", help="Run a stdio MCP server exposing rewind tools.")
    mcp.add_argument("--root", dest="mcp_root", default=None, help="Default project root for MCP tools.")
    args = parser.parse_args()
    project = None
    try:
        if args.action == "hook":
            target = "claude-strict" if args.strict and args.target == "claude" else args.target
            print(HOOKS[target])
            return 0
        if args.action == "mcp":
            from mcp_server import serve

            serve(default_root=args.mcp_root or args.root, default_store=args.store)
            return 0
        project = Project(args.root, args.store)
        if args.action == "status":
            info = project.stats()
            if args.json:
                print(json.dumps(info, ensure_ascii=False, indent=2))
            else:
                extra = []
                if info["failed"]:
                    extra.append(f"{info['failed']} failed")
                if info["corrupt"]:
                    extra.append(f"{info['corrupt']} corrupt")
                notes = f", {', '.join(extra)}" if extra else ""
                print(
                    f"Project: {info['root']}\n"
                    f"Store:   {info['store']}\n"
                    f"Records: {info['records']} ({info['pending']} pending{notes})\n"
                    f"Objects: {info['objects']} ({info['bytes']} bytes)"
                )
            return 0
        if args.action in ("list", "ui", None):
            records = project.records()
            if args.action == "list":
                if args.agent:
                    records = [rec for rec in records if rec.get("agent") == args.agent]
                if args.session:
                    records = [rec for rec in records if rec.get("session") == args.session]
                records = records[:max(1, args.limit)]
            else:
                records = records[:30]
            if args.action == "list" and args.json:
                payload = [
                    {
                        "id": rec["id"],
                        "created": rec["created"],
                        "kind": rec["kind"],
                        "state": rec["state"],
                        "agent": rec.get("agent"),
                        "session": rec.get("session"),
                        "step": rec.get("step"),
                        "call_id": rec.get("call_id"),
                        "batch": rec.get("batch"),
                        "concurrent": bool(rec.get("concurrent")),
                        "cancelled": bool(rec.get("cancelled")),
                        "exit_code": rec.get("exit_code"),
                        "label": rec["label"],
                        "changes": list(rec.get("changes", {})),
                    }
                    for rec in records
                ]
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.action != "list" and sys.stdin.isatty() and sys.stdout.isatty():
                from timeline import show

                show(project, records)
            else:
                print("\n".join(map(summary, records)) or "No history. Start with rewind run -- <program>.")
            return 0
        if args.action == "run":
            return run_command(project, args.command)
        if args.action == "begin":
            hook = read_claude_hook_context() if args.claude_hook else {}
            agent = args.agent or os.environ.get("TERM_REWIND_AGENT")
            session = args.session or hook.get("session_id") or os.environ.get("TERM_REWIND_SESSION")
            call_id = args.call_id or hook.get("tool_use_id") or hook.get("call_id")
            label = args.label
            if args.claude_hook and hook.get("tool_name") and label == "claude: tool":
                label = f"claude: {hook['tool_name']}"
            with project.lock():
                rec = project.start(
                    label, "task", agent=agent, session=session,
                    step=1 if session else None, call_id=call_id,
                )
                print(rec["id"])
            return 0
        if args.action == "step":
            agent = args.agent or os.environ.get("TERM_REWIND_AGENT")
            session = args.session or os.environ.get("TERM_REWIND_SESSION")
            with project.lock():
                closed, opened = project.step(args.label, agent=agent, session=session)
            if closed is not None:
                print(summary(closed))
            print(opened["id"])
            return 0
        if args.action == "end":
            hook = read_claude_hook_context() if args.claude_hook else {}
            session = args.session or hook.get("session_id")
            call_id = args.call_id or hook.get("tool_use_id") or hook.get("call_id")
            exit_code = args.exit_code
            hook_error = hook.get("error")
            if args.claude_hook and exit_code is None and (
                hook.get("hook_event_name") == "PostToolUseFailure" or hook_error
            ):
                exit_code = 1
            with project.lock():
                record = (
                    project.get(args.id) if args.id
                    else project.latest_pending(session=session, call_id=call_id)
                )
                if record["state"] == "pending" and "planned" in record:
                    print(summary(project.recover_undo(record)))
                else:
                    print(summary(project.finish(record, exit_code=exit_code, error=hook_error)))
            return 0
        if args.action == "gc":
            with project.lock():
                removed_records, removed_objects, freed = project.gc(keep=args.keep)
            print(f"Removed {removed_records} record(s), {removed_objects} object(s) ({freed} bytes freed).")
            return 0

        record = project.get(args.id) if args.id else project.latest_complete()
        changes = get_changes(record)
        paths = select_paths(changes, args.file if args.file else None)
        if args.action == "diff":
            if args.json:
                payload = {
                    "id": record["id"],
                    "summary": summary(record),
                    "diffs": {path: file_diff(project, path, *changes[path]) for path in paths},
                    "skipped": record.get("skipped", {}),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            print(summary(record))
            for path in paths:
                print("\n".join(file_diff(project, path, *changes[path])))
            if not paths:
                print("No recorded file changes.")
            for path, reason in record["skipped"].items():
                print(f"Skipped {safe_text(path)}: {reason}")
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                raise RewindError("Use --yes after reviewing the diff to undo without a terminal.")
            if input(f"Undo {len(paths)} file change(s) in {record['id']}? [y/N] ").lower() != "y":
                return 0
        with project.lock():
            print(summary(project.undo(record, paths, force=args.force)))
        return 0
    except (RewindError, OSError, ValueError) as error:
        if args.action in ("begin", "step", "end") and (
            getattr(args, "warn_only", False) or getattr(args, "strict", False)
        ):
            if project is not None and args.action in ("begin", "step"):
                agent = getattr(args, "agent", None) or os.environ.get("TERM_REWIND_AGENT")
                session = getattr(args, "session", None) or os.environ.get("TERM_REWIND_SESSION")
                try:
                    project.record_failure(args.label, "task", error, agent=agent, session=session)
                except (OSError, RewindError):
                    pass
            print(
                f"rewind: WARNING - snapshot NOT recorded ({error}). Subsequent edits are NOT protected.",
                file=sys.stderr,
            )
            return 2 if getattr(args, "strict", False) else 0
        print(f"rewind: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    return cli()


if __name__ == "__main__":
    sys.modules["rewind"] = sys.modules[__name__]
    raise SystemExit(main())
