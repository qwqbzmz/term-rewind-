import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_server
import rewind


class RewindTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        base = Path(self.temp_dir)
        self.project_dir = base / "project"
        self.store_dir = base / "store"
        self.project_dir.mkdir()
        self.project = rewind.Project(self.project_dir, self.store_dir)

    def tearDown(self):
        shutil.rmtree(rewind.win_path(self.temp_dir), ignore_errors=True)

    def test_capture_and_stat_cache_reuse_and_invalidation(self):
        target = self.project_dir / "note.txt"
        target.write_text("v1\n", encoding="utf-8")

        # Age timestamps beyond the racy window so the second capture hits stat-cache.
        old_ns = time.time_ns() - (rewind.RACY_WINDOW_NS * 4)
        os.utime(target, ns=(old_ns, old_ns))

        first = self.project.capture()
        key_v1 = first["files"]["note.txt"]
        cache = self.project.load_stat_cache()
        self.assertIn("note.txt", cache)

        # Even if object bytes are untouched, stat-cache hit avoids re-reading when unchanged,
        # and modifying the file invalidates the cache entry.
        second = self.project.capture()
        self.assertEqual(second["files"]["note.txt"], key_v1)

        target.write_text("v2\n", encoding="utf-8")
        third = self.project.capture()
        self.assertNotEqual(third["files"]["note.txt"], key_v1)

    def test_rewindignore_excludes_matching_paths(self):
        (self.project_dir / ".rewindignore").write_text("*.log\ndist/\n", encoding="utf-8")
        (self.project_dir / "keep.txt").write_text("ok", encoding="utf-8")
        (self.project_dir / "debug.log").write_text("ignore me", encoding="utf-8")
        dist = self.project_dir / "dist"
        dist.mkdir()
        (dist / "bundle.js").write_text("ignore dir", encoding="utf-8")

        snap = self.project.capture()
        self.assertIn("keep.txt", snap["files"])
        self.assertEqual(snap["skipped"]["debug.log"], "excluded")
        self.assertEqual(snap["skipped"]["dist"], "excluded")

    def test_selective_undo_conflict_and_force(self):
        config = self.project_dir / "config.json"
        draft = self.project_dir / "draft.md"
        config.write_text('{"mode": "original"}\n', encoding="utf-8")
        draft.write_text("important draft\n", encoding="utf-8")

        record = self.project.start("simulate bad edit", "task")
        config.write_text('{"mode": "broken"}\n', encoding="utf-8")
        draft.unlink()
        (self.project_dir / "extra.tmp").write_text("temp", encoding="utf-8")
        self.project.finish(record, exit_code=1)

        # User edits config.json afterwards -> conflict on full undo.
        config.write_text('{"mode": "manual"}\n', encoding="utf-8")
        with self.assertRaises(rewind.RewindError) as ctx:
            self.project.undo(record, ["config.json", "draft.md"])
        self.assertIn("Conflicts", str(ctx.exception))
        self.assertFalse(draft.exists())

        # Selective undo of draft.md + extra.tmp succeeds without touching config.json.
        self.project.undo(record, ["draft.md", "extra.tmp"])
        self.assertEqual(draft.read_text(encoding="utf-8"), "important draft\n")
        self.assertFalse((self.project_dir / "extra.tmp").exists())
        self.assertEqual(config.read_text(encoding="utf-8"), '{"mode": "manual"}\n')

        # Force undo overwrites config.json back to original, and is itself reversible.
        forced = self.project.undo(record, ["config.json"], force=True)
        self.assertEqual(config.read_text(encoding="utf-8"), '{"mode": "original"}\n')
        self.project.undo(forced, ["config.json"])
        self.assertEqual(config.read_text(encoding="utf-8"), '{"mode": "manual"}\n')

    def test_gc_prunes_old_records_and_unreferenced_objects(self):
        file_path = self.project_dir / "counter.txt"
        for idx in range(5):
            rec = self.project.start(f"step-{idx}", "task")
            file_path.write_text(f"value-{idx}\n", encoding="utf-8")
            self.project.finish(rec, exit_code=0)

        # Keep one pending record to verify gc never deletes pending records.
        pending = self.project.start("open-task", "task")

        removed_recs, removed_objs, freed = self.project.gc(keep=2)
        self.assertEqual(removed_recs, 3)
        self.assertGreaterEqual(removed_objs, 1)
        self.assertGreater(freed, 0)
        remaining_ids = {r["id"] for r in self.project.records()}
        self.assertIn(pending["id"], remaining_ids)
        self.assertEqual(len(remaining_ids), 3)

    def test_cli_begin_end_latest_and_hook(self):
        (self.project_dir / "a.txt").write_text("before", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self._run_cli(["--root", str(self.project_dir), "--store", str(self.store_dir), "begin", "agent"])
        self.assertEqual(code, 0)
        rec_id = out.getvalue().strip()
        self.assertEqual(len(rec_id), 12)

        (self.project_dir / "a.txt").write_text("after", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self._run_cli(["--root", str(self.project_dir), "--store", str(self.store_dir), "end"])
        self.assertEqual(code, 0)
        self.assertIn(rec_id, out.getvalue())

        hook_out = io.StringIO()
        with contextlib.redirect_stdout(hook_out):
            code = self._run_cli(["hook", "claude"])
        self.assertEqual(code, 0)
        self.assertIn("PreToolUse", hook_out.getvalue())

    def test_status_and_json_cli_outputs(self):
        target = self.project_dir / "main.py"
        target.write_text("print(1)\n", encoding="utf-8")
        (self.project_dir / ".next").mkdir()
        (self.project_dir / ".next" / "cache.bin").write_text("x", encoding="utf-8")

        rec = self.project.start("edit main", "task")
        target.write_text("print(2)\n", encoding="utf-8")
        self.project.finish(rec, exit_code=0)

        for argv, check in (
            (["--root", str(self.project_dir), "--store", str(self.store_dir), "status", "--json"],
             lambda data: self.assertEqual(data["records"], 1)),
            (["--root", str(self.project_dir), "--store", str(self.store_dir), "list", "--json"],
             lambda data: self.assertEqual(data[0]["id"], rec["id"])),
            (["--root", str(self.project_dir), "--store", str(self.store_dir), "diff", rec["id"], "--json"],
             lambda data: self.assertIn("main.py", data["diffs"])),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self._run_cli(argv)
            self.assertEqual(code, 0)
            check(json.loads(out.getvalue()))

        snap = self.project.capture()
        self.assertEqual(snap["skipped"].get(".next"), "excluded")

    def test_mcp_server_tool_workflow(self):
        doc = self.project_dir / "README.txt"
        doc.write_text("hello\n", encoding="utf-8")

        init_resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "term-rewind")

        tools_resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools_resp["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {"rewind_begin", "rewind_step", "rewind_end", "rewind_list", "rewind_diff", "rewind_undo"},
        )

        begin_resp = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "rewind_begin", "arguments": {"label": "mcp edit"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
        )
        self.assertFalse(begin_resp["result"]["isError"])
        rec_id = json.loads(begin_resp["result"]["content"][0]["text"])["id"]

        doc.write_text("broken\n", encoding="utf-8")
        end_resp = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "rewind_end", "arguments": {}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
        )
        self.assertIn("README.txt", json.loads(end_resp["result"]["content"][0]["text"])["changes"])

        undo_resp = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "rewind_undo", "arguments": {"id": rec_id, "all": True}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
        )
        self.assertFalse(undo_resp["result"]["isError"])
        self.assertEqual(doc.read_text(encoding="utf-8"), "hello\n")

    def test_agent_attribution_and_step_checkpoints(self):
        app_file = self.project_dir / "app.py"
        app_file.write_text("v0\n", encoding="utf-8")

        state = {}
        mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "cursor"}}},
            state=state,
        )
        self.assertEqual(state["agent"], "cursor")

        mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "rewind_begin", "arguments": {"label": "thought 1", "session": "s1"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
            state=state,
        )
        app_file.write_text("v1\n", encoding="utf-8")

        step_resp = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "rewind_step", "arguments": {"label": "thought 2", "session": "s1"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
            state=state,
        )
        step_payload = json.loads(step_resp["result"]["content"][0]["text"])
        self.assertEqual(step_payload["closed"]["changes"], ["app.py"])
        self.assertEqual(step_payload["opened"]["agent"], "cursor")
        self.assertEqual(step_payload["opened"]["step"], 2)

        app_file.write_text("v2\n", encoding="utf-8")
        mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "rewind_end", "arguments": {"session": "s1"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
            state=state,
        )

        # CLI filter by --agent returns only matching records.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self._run_cli(
                ["--root", str(self.project_dir), "--store", str(self.store_dir), "list", "--agent", "cursor", "--json"]
            )
        self.assertEqual(code, 0)
        listed = json.loads(out.getvalue())
        self.assertEqual(len(listed), 2)
        self.assertEqual([item["step"] for item in listed], [2, 1])

    def test_prefix_lookup_and_default_latest_diff_undo(self):
        target = self.project_dir / "file.txt"
        target.write_text("v1\n", encoding="utf-8")
        rec = self.project.start("edit", "task")
        target.write_text("v2\n", encoding="utf-8")
        self.project.finish(rec, exit_code=0)

        # Prefix lookup resolves unique prefix cleanly.
        matched = self.project.get(rec["id"][:5])
        self.assertEqual(matched["id"], rec["id"])

        # Ambiguous prefix is rejected cleanly.
        rec2 = self.project.start("edit2", "task")
        rec2["id"] = rec["id"][:4] + "ffffffff"
        self.project.save(rec2)
        with self.assertRaises(rewind.RewindError):
            self.project.get(rec["id"][:4])

        # `diff` and `undo` with no ID operate on the latest completed record.
        diff_out = io.StringIO()
        with contextlib.redirect_stdout(diff_out):
            code = self._run_cli(["--root", str(self.project_dir), "--store", str(self.store_dir), "diff"])
        self.assertEqual(code, 0)
        self.assertIn("file.txt", diff_out.getvalue())

        undo_out = io.StringIO()
        with contextlib.redirect_stdout(undo_out):
            code = self._run_cli(["--root", str(self.project_dir), "--store", str(self.store_dir), "undo", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "v1\n")

    def test_deep_paths_exceeding_windows_max_path(self):
        deep_base = Path(self.temp_dir) / ("nested_folder_" * 7) / ("deeper_" * 7)
        deep_proj = deep_base / "demo-data" / "demo-12345678" / "project"
        deep_store = deep_base / "demo-data" / "demo-12345678" / "snapshots"
        rewind.win_path(deep_proj).mkdir(parents=True, exist_ok=True)
        rewind.win_path(deep_proj / "draft.md").write_text("deep draft\n", encoding="utf-8")

        proj = rewind.Project(deep_proj, deep_store)
        sample_obj = proj.object_path("a" * 64)
        self.assertGreater(len(str(sample_obj)), 280)

        rec = proj.start("deep edit", "task")
        rewind.win_path(deep_proj / "draft.md").unlink()
        proj.finish(rec, exit_code=1)
        diff_lines = rewind.file_diff(proj, "draft.md", *rec["changes"]["draft.md"])
        self.assertIn("-deep draft", "\n".join(diff_lines))

        proj.undo(rec, ["draft.md"])
        self.assertEqual(rewind.win_path(deep_proj / "draft.md").read_text(encoding="utf-8"), "deep draft\n")
        proj.gc(keep=1)
        self.assertGreaterEqual(proj.stats()["objects"], 1)

    def test_mcp_stdio_subprocess_wire_protocol(self):
        target = self.project_dir / "wire.txt"
        target.write_text("before\n", encoding="utf-8")

        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "rewind.py"),
                "--root",
                str(self.project_dir),
                "--store",
                str(self.store_dir),
                "mcp",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            def send_line(payload):
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                proc.stdin.flush()
                return json.loads(proc.stdout.readline().decode("utf-8"))

            init_resp = send_line(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "real-agent"}}}
            )
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "term-rewind")

            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()

            begin_resp = send_line(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "rewind_begin", "arguments": {"label": "wire test"}},
                }
            )
            begin_data = json.loads(begin_resp["result"]["content"][0]["text"])
            self.assertEqual(begin_data["agent"], "real-agent")

            target.write_text("after\n", encoding="utf-8")
            end_resp = send_line(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rewind_end", "arguments": {}}}
            )
            end_data = json.loads(end_resp["result"]["content"][0]["text"])
            self.assertEqual(end_data["changes"], ["wire.txt"])

            undo_resp = send_line(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "rewind_undo", "arguments": {"id": begin_data["id"], "all": True}},
                }
            )
            self.assertFalse(undo_resp["result"]["isError"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

            # Also verify Content-Length framing over the same live stdio pipe.
            ping_body = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode("utf-8")
            proc.stdin.write(f"Content-Length: {len(ping_body)}\r\n\r\n".encode("ascii") + ping_body)
            proc.stdin.flush()
            header = proc.stdout.readline().decode("ascii").strip()
            self.assertTrue(header.lower().startswith("content-length:"))
            length = int(header.split(":", 1)[1].strip())
            _blank = proc.stdout.readline()
            ping_resp = json.loads(proc.stdout.read(length).decode("utf-8"))
            self.assertEqual(ping_resp["id"], 5)
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            stderr = proc.stderr.read().decode("utf-8", errors="replace")
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(proc.returncode, 0, msg=stderr)

    def test_corrupted_object_and_corrupted_history_json(self):
        doc = self.project_dir / "doc.txt"
        doc.write_text("clean\n", encoding="utf-8")
        rec = self.project.start("edit", "task")
        doc.write_text("edited\n", encoding="utf-8")
        self.project.finish(rec, exit_code=0)

        # Tamper with stored object bytes -> undo must fail integrity check before writing.
        obj_key = rec["changes"]["doc.txt"][0]
        obj_path = self.project.object_path(obj_key)
        obj_path.write_bytes(b"corrupted!")
        with self.assertRaises(rewind.RewindError) as ctx:
            self.project.undo(rec, ["doc.txt"])
        self.assertIn("integrity check", str(ctx.exception))
        self.assertEqual(doc.read_text(encoding="utf-8"), "edited\n")

        # Write a corrupt JSON history file -> records() surfaces it without crashing list/status.
        bad_json = rewind.win_path(self.project.store / "history" / "badrecord.json")
        bad_json.write_text("{broken json", encoding="utf-8")
        records = self.project.records()
        states = {r["id"]: r["state"] for r in records}
        self.assertEqual(states.get("badrecord"), "corrupt")
        self.assertEqual(self.project.stats()["corrupt"], 1)

    def test_partial_undo_and_recover_undo(self):
        f1 = self.project_dir / "a.txt"
        f2 = self.project_dir / "b.txt"
        f1.write_text("a1\n", encoding="utf-8")
        f2.write_text("b1\n", encoding="utf-8")
        rec = self.project.start("modify both", "task")
        f1.write_text("a2\n", encoding="utf-8")
        f2.write_text("b2\n", encoding="utf-8")
        self.project.finish(rec, exit_code=0)

        # Simulate mid-undo failure on the second file after the first file is already written.
        orig_atomic_write = rewind.atomic_write
        calls = {"count": 0}

        def flaky_write(path, data):
            if Path(path).name == "b.txt":
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("simulated disk failure")
            return orig_atomic_write(path, data)

        with mock.patch.object(rewind, "atomic_write", side_effect=flaky_write):
            with self.assertRaises(rewind.RewindError) as ctx:
                self.project.undo(rec, ["a.txt", "b.txt"])
        self.assertIn("Undo stopped after 1 file(s)", str(ctx.exception))
        self.assertEqual(f1.read_text(encoding="utf-8"), "a1\n")
        self.assertEqual(f2.read_text(encoding="utf-8"), "b2\n")

        # Verify partial undo record was saved and recover_undo reconciles accurately.
        partial = next(r for r in self.project.records() if r["state"] == "partial")
        self.assertEqual(list(partial["changes"]), ["a.txt"])

        # Also test recovering an interrupted pending undo record directly.
        interrupted = rewind.new_record("Undo interrupted", "undo")
        interrupted["planned"] = {
            "a.txt": [rec["changes"]["a.txt"][1], rec["changes"]["a.txt"][0]],
            "b.txt": [rec["changes"]["b.txt"][1], rec["changes"]["b.txt"][0]],
        }
        self.project.save(interrupted)
        recovered = self.project.recover_undo(interrupted)
        self.assertEqual(recovered["state"], "partial")
        self.assertEqual(list(recovered["changes"]), ["a.txt"])

    def test_symlink_junction_and_path_traversal_protection(self):
        for bad in ("../escape", "sub/../../escape", "a\\b", "C:drive"):
            with self.assertRaises(rewind.RewindError):
                self.project.target(bad)

        outside = Path(self.temp_dir) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")

        link_dir = self.project_dir / "linked"
        created_link = False
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(outside)],
                capture_output=True,
                text=True,
            )
            created_link = res.returncode == 0
        else:
            try:
                os.symlink(outside, link_dir)
                created_link = True
            except OSError:
                created_link = False

        if created_link:
            snap = self.project.capture()
            self.assertEqual(snap["skipped"].get("linked"), "link or reparse point")
            with self.assertRaises(rewind.RewindError):
                self.project.target("linked/secret.txt")

    def test_snapshot_file_and_project_limits(self):
        small = self.project_dir / "small.txt"
        small.write_text("ok", encoding="utf-8")
        big = self.project_dir / "big.bin"
        # Create a sparse-like >8 MiB file to verify FILE_LIMIT skip without reading huge bytes.
        with big.open("wb") as handle:
            handle.seek(rewind.FILE_LIMIT + 10)
            handle.write(b"x")

        snap = self.project.capture()
        self.assertIn("small.txt", snap["files"])
        self.assertEqual(snap["skipped"].get("big.bin"), "over 8 MiB")

        # Temporarily lower PROJECT_LIMIT to test project-level size rejection.
        (self.project_dir / "medium.txt").write_bytes(b"x" * 2048)
        with mock.patch.object(rewind, "PROJECT_LIMIT", 1024):
            with self.assertRaises(rewind.RewindError) as ctx:
                self.project.capture()
        self.assertIn("Snapshot exceeds", str(ctx.exception))

    def test_hook_warn_and_strict_mode_failure_recording(self):
        (self.project_dir / "data.bin").write_bytes(b"x" * 2048)

        # Warn mode: exits 0, emits prominent warning to stderr, records failed entry.
        with mock.patch.object(rewind, "PROJECT_LIMIT", 1024):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                code = self._run_cli(
                    [
                        "--root", str(self.project_dir),
                        "--store", str(self.store_dir),
                        "begin", "claude: tool",
                        "--agent", "claude",
                        "--warn-only",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn("WARNING - snapshot NOT recorded", err_buf.getvalue())
        records = self.project.records()
        self.assertEqual(records[0]["state"], "failed")
        self.assertEqual(records[0]["agent"], "claude")
        self.assertEqual(self.project.stats()["failed"], 1)

        # Strict mode: exits 2 (blocks Claude Code PreToolUse) and records failure.
        with mock.patch.object(rewind, "PROJECT_LIMIT", 1024):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                code = self._run_cli(
                    [
                        "--root", str(self.project_dir),
                        "--store", str(self.store_dir),
                        "begin", "claude: tool",
                        "--agent", "claude",
                        "--strict",
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("NOT protected", err_buf.getvalue())

        # Strict hook snippet uses --strict.
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            code = self._run_cli(["hook", "claude", "--strict"])
        self.assertEqual(code, 0)
        self.assertIn("--strict", out_buf.getvalue())

    def test_desktop_app_and_floating_smoke(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter not available")
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"display unavailable: {error}")

        root.withdraw()
        app_path = ROOT / "app.pyw"
        loader = importlib.machinery.SourceFileLoader("term_rewind_app", str(app_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(app_mod)

        app = None
        try:
            app = app_mod.RewindApp(root, settings_path=Path(self.temp_dir) / "desktop.json")
            # Prepare project directly and exercise UI rendering, floating capsule, restore, and gc.
            draft = self.project_dir / "draft.md"
            draft.write_text("desktop draft\n", encoding="utf-8")
            rec = self.project.start("desktop cmd", "task", agent="claude", step=1)
            draft.unlink()
            self.project.finish(rec, exit_code=1)

            app.set_project(self.project, rec["id"])
            root.update_idletasks()
            self.assertIn("1 条记录", app.storage_usage.get())
            self.assertIn("[claude#1]", app.history.item(rec["id"], "text"))

            # Select draft.md and verify preview + floating capsule pages.
            for item, (path, _reason) in app.paths.items():
                if path == "draft.md":
                    app.files.selection_set(item)
                    app.on_file()
            app.float_enabled.set(True)
            app.toggle_floating()
            root.update_idletasks()
            self.assertIsNotNone(app.floating)
            app.floating.expand(True)
            app.float_pages.open("history")
            app.float_pages.files(rec["id"], ["draft.md"])
            root.update_idletasks()

            with self.project.lock():
                self.project.undo(rec, ["draft.md"])
            app.refresh()
            self.assertEqual(draft.read_text(encoding="utf-8"), "desktop draft\n")

            with self.project.lock():
                self.project.gc(keep=20)
            app.update_storage_info()
            root.update_idletasks()
        finally:
            if app is not None:
                app.quit_app()
            else:
                root.destroy()

    def test_concurrent_agent_pairing_and_claude_hook_failure(self):
        a_file = self.project_dir / "a.txt"
        b_file = self.project_dir / "b.txt"
        a_file.write_text("a0\n", encoding="utf-8")
        b_file.write_text("b0\n", encoding="utf-8")

        # Start A via MCP call_id and B via Claude PreToolUse stdin JSON before A finishes.
        resp_a = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "rewind_begin", "arguments": {"label": "op A", "agent": "claude", "call_id": "call-A"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
        )
        id_a = json.loads(resp_a["result"]["content"][0]["text"])["id"]

        hook_begin_b = json.dumps({
            "session_id": "sess-1",
            "tool_use_id": "call-B",
            "tool_name": "Edit",
            "hook_event_name": "PreToolUse",
        })
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(hook_begin_b)
            out_b = io.StringIO()
            with contextlib.redirect_stdout(out_b):
                code = self._run_cli([
                    "--root", str(self.project_dir), "--store", str(self.store_dir),
                    "begin", "claude: tool", "--agent", "claude", "--claude-hook",
                ])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        id_b = out_b.getvalue().strip()

        # Overlapping edit: A writes a.txt and ends first; B must stay pending!
        a_file.write_text("a1\n", encoding="utf-8")
        end_a = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "rewind_end", "arguments": {"call_id": "call-A"}},
            },
            default_root=str(self.project_dir),
            default_store=self.store_dir,
        )
        end_a_data = json.loads(end_a["result"]["content"][0]["text"])
        self.assertEqual(end_a_data["id"], id_a)
        self.assertEqual(end_a_data["changes"], ["a.txt"])
        self.assertTrue(end_a_data["concurrent"])
        self.assertEqual(self.project.get(id_b)["state"], "pending")

        # B modifies b.txt and finishes via PostToolUseFailure JSON.
        b_file.write_text("b1\n", encoding="utf-8")
        hook_fail_b = json.dumps({
            "session_id": "sess-1",
            "tool_use_id": "call-B",
            "tool_name": "Edit",
            "hook_event_name": "PostToolUseFailure",
            "error": "tool crashed",
        })
        try:
            sys.stdin = io.StringIO(hook_fail_b)
            with contextlib.redirect_stdout(io.StringIO()):
                code = self._run_cli([
                    "--root", str(self.project_dir), "--store", str(self.store_dir),
                    "end", "--claude-hook", "--warn-only",
                ])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)

        rec_a = self.project.get(id_a)
        rec_b = self.project.get(id_b)
        self.assertEqual(rec_b["state"], "complete")
        self.assertEqual(rec_b["exit_code"], 1)
        self.assertEqual(rec_b["error"], "tool crashed")
        self.assertTrue(rec_a["concurrent"])
        self.assertTrue(rec_b["concurrent"])
        self.assertEqual(rec_a["batch"], rec_b["batch"])

    def test_mcp_undo_safety_guards_and_dry_run(self):
        f1 = self.project_dir / "one.txt"
        f2 = self.project_dir / "two.txt"
        f1.write_text("1-orig\n", encoding="utf-8")
        f2.write_text("2-orig\n", encoding="utf-8")

        rec = self.project.start("two files", "task")
        f1.write_text("1-changed\n", encoding="utf-8")
        f2.write_text("2-changed\n", encoding="utf-8")
        self.project.finish(rec, exit_code=0)

        # Simulate later external edit creating a conflict on one.txt.
        f1.write_text("1-external\n", encoding="utf-8")

        # Empty files list must error, not undo all files.
        empty_resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "rewind_undo", "arguments": {"id": rec["id"], "files": []}}},
            default_root=str(self.project_dir), default_store=self.store_dir,
        )
        self.assertTrue(empty_resp["result"]["isError"])
        self.assertEqual(f2.read_text(encoding="utf-8"), "2-changed\n")

        # Omitted files without all=True must error.
        implicit_resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rewind_undo", "arguments": {"id": rec["id"]}}},
            default_root=str(self.project_dir), default_store=self.store_dir,
        )
        self.assertTrue(implicit_resp["result"]["isError"])
        self.assertEqual(f2.read_text(encoding="utf-8"), "2-changed\n")

        # dry_run returns planned paths and conflicts without modifying files.
        dry_resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "rewind_undo", "arguments": {"id": rec["id"], "all": True, "dry_run": True}}},
            default_root=str(self.project_dir), default_store=self.store_dir,
        )
        self.assertFalse(dry_resp["result"]["isError"])
        dry_data = json.loads(dry_resp["result"]["content"][0]["text"])
        self.assertTrue(dry_data["dry_run"])
        self.assertEqual(dry_data["paths"], ["one.txt", "two.txt"])
        self.assertEqual(len(dry_data["conflicts"]), 1)
        self.assertIn("one.txt", dry_data["conflicts"][0])
        self.assertEqual(f2.read_text(encoding="utf-8"), "2-changed\n")

        # Explicit subset undo skips conflicting one.txt and restores two.txt.
        ok_resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "rewind_undo", "arguments": {"id": rec["id"], "files": ["two.txt"]}}},
            default_root=str(self.project_dir), default_store=self.store_dir,
        )
        self.assertFalse(ok_resp["result"]["isError"])
        self.assertEqual(f2.read_text(encoding="utf-8"), "2-orig\n")
        self.assertEqual(f1.read_text(encoding="utf-8"), "1-external\n")

    def test_desktop_pagination_filters_and_live_cancel(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter not available")
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"display unavailable: {error}")

        import time
        root.withdraw()
        app_path = ROOT / "app.pyw"
        loader = importlib.machinery.SourceFileLoader("term_rewind_app_live", str(app_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(app_mod)

        app = None
        try:
            # Create 35 records so pagination (>30), search, and failed filtering are exercised.
            for idx in range(35):
                rec = rewind.new_record(f"task-{idx:02d}", "task")
                rec["state"] = "complete"
                rec["order"] = idx + 1
                rec["exit_code"] = 2 if idx == 5 else 0
                rec["changes"] = {f"file_{idx:02d}.txt": [None, "a" * 64]}
                self.project.save(rec)

            app = app_mod.RewindApp(root, settings_path=Path(self.temp_dir) / "desktop2.json")
            app.set_project(self.project)
            root.update_idletasks()
            self.assertEqual(len(app.history.get_children()), 30)
            self.assertIn("30 / 35", app.history_summary.get())

            app.load_more_records()
            root.update_idletasks()
            self.assertEqual(len(app.history.get_children()), 35)

            app.search_query.set("file_03.txt")
            root.update_idletasks()
            self.assertEqual(len(app.history.get_children()), 1)

            app.search_query.set("")
            app.only_failed.set(True)
            root.update_idletasks()
            self.assertEqual(len(app.history.get_children()), 1)
            app.only_failed.set(False)
            root.update_idletasks()

            # Test live streaming command output + cancel saving partial changes.
            script = self.project_dir / "long_edit.py"
            script.write_text(
                "import time, pathlib\n"
                "pathlib.Path('partial.txt').write_text('partial-data\\n', encoding='utf-8')\n"
                "print('LIVE_MARKER_LINE', flush=True)\n"
                "time.sleep(10)\n",
                encoding="utf-8",
            )
            python_exe = Path(sys.executable)
            if python_exe.name.lower() == "pythonw.exe":
                python_exe = python_exe.with_name("python.exe")

            done_ids = []
            app.run_line(
                subprocess.list2cmdline([str(python_exe), str(script)]),
                completed=done_ids.append,
            )
            deadline = time.monotonic() + 10.0
            saw_live = False
            while time.monotonic() < deadline and not done_ids:
                root.update()
                if app.active_record_id and "LIVE_MARKER_LINE" in app.outputs.get(app.active_record_id, ""):
                    saw_live = True
                    app.cancel_command()
                    break
                time.sleep(0.02)
            while time.monotonic() < deadline and (app.busy or not done_ids):
                root.update()
                time.sleep(0.02)

            self.assertTrue(saw_live)
            self.assertEqual(len(done_ids), 1)
            cancelled_rec = self.project.get(done_ids[0])
            self.assertEqual(cancelled_rec["state"], "complete")
            self.assertTrue(cancelled_rec.get("cancelled"))
            self.assertIn("partial.txt", cancelled_rec["changes"])
            self.assertTrue((self.project_dir / "partial.txt").is_file())
        finally:
            if app is not None:
                app.quit_app()
            else:
                root.destroy()

    def _run_cli(self, argv):
        old_argv = sys.argv
        try:
            sys.argv = ["rewind", *argv]
            return rewind.cli()
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
