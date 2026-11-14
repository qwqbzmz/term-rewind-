"""Zero-dependency stdio MCP server for term-rewind."""

import json
import sys

from rewind import (
    Project,
    RewindError,
    default_storage,
    file_diff,
    get_changes,
    select_paths,
    summary,
)


SERVER_INFO = {"name": "term-rewind", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "rewind_begin",
        "description": "Take a baseline snapshot of the project before making edits or running commands.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Short task description."},
                "agent": {"type": "string", "description": "Agent identifier (defaults to MCP client name)."},
                "session": {"type": "string", "description": "Optional session/task group ID."},
                "call_id": {"type": "string", "description": "Optional tool call ID to pair begin/end for concurrent calls."},
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "rewind_step",
        "description": "Close the current pending step (recording its diff) and start the next checkpoint in one scan.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Description of the next reasoning/edit step."},
                "agent": {"type": "string", "description": "Agent identifier (defaults to MCP client name)."},
                "session": {"type": "string", "description": "Optional session/task group ID."},
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "rewind_end",
        "description": "Finish a pending snapshot and return the recorded file changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Record ID (defaults to latest pending record)."},
                "session": {"type": "string", "description": "Optional session ID when closing the latest pending step."},
                "call_id": {"type": "string", "description": "Optional tool call ID matching the begin call."},
                "exit_code": {"type": "integer", "description": "Optional command exit code."},
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
        },
    },
    {
        "name": "rewind_list",
        "description": "List recent snapshot records for the project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max records to return (default 10)."},
                "agent": {"type": "string", "description": "Filter records by agent name."},
                "session": {"type": "string", "description": "Filter records by session ID."},
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
        },
    },
    {
        "name": "rewind_diff",
        "description": "Get unified diffs for a recorded operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Record ID."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of relative paths (omit for all files).",
                },
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "rewind_undo",
        "description": (
            "Selectively restore files to their state before a recorded operation. "
            "Requires non-empty 'files' or explicit 'all': true; supports 'dry_run': true to preview paths and conflicts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Record ID to undo."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Non-empty list of relative paths to restore.",
                },
                "all": {"type": "boolean", "description": "Explicitly confirm restoring all changed files in the record."},
                "dry_run": {"type": "boolean", "description": "Preview target paths and conflicts without modifying files."},
                "force": {"type": "boolean", "description": "Overwrite even if files changed afterwards."},
                "root": {"type": "string", "description": "Project directory (defaults to server root)."},
            },
            "required": ["id"],
        },
    },
]


def _call_tool(name, args, default_root, default_store, state=None):
    args = args or {}
    state = state or {}
    project = Project(args.get("root") or default_root, default_store)
    default_agent = state.get("agent")
    if name == "rewind_begin":
        agent = args.get("agent") or default_agent
        session = args.get("session")
        call_id = args.get("call_id")
        with project.lock():
            record = project.start(
                str(args["label"]), "task",
                agent=agent, session=session, step=1 if session else None, call_id=call_id,
            )
        return {
            "id": record["id"], "label": record["label"], "state": record["state"],
            "agent": record.get("agent"), "session": record.get("session"), "step": record.get("step"),
            "call_id": record.get("call_id"), "batch": record.get("batch"),
            "concurrent": bool(record.get("concurrent")),
        }
    if name == "rewind_step":
        agent = args.get("agent") or default_agent
        session = args.get("session")
        with project.lock():
            closed, opened = project.step(str(args["label"]), agent=agent, session=session)
        return {
            "closed": {"id": closed["id"], "summary": summary(closed), "changes": list(closed.get("changes", {}))} if closed else None,
            "opened": {"id": opened["id"], "label": opened["label"], "agent": opened.get("agent"), "session": opened.get("session"), "step": opened.get("step")},
        }
    if name == "rewind_end":
        with project.lock():
            record = (
                project.get(args["id"]) if args.get("id")
                else project.latest_pending(session=args.get("session"), call_id=args.get("call_id"))
            )
            if record["state"] == "pending" and "planned" in record:
                done = project.recover_undo(record)
            else:
                done = project.finish(record, exit_code=args.get("exit_code"))
        return {
            "id": done["id"], "summary": summary(done), "changes": list(done.get("changes", {})),
            "call_id": done.get("call_id"), "batch": done.get("batch"),
            "concurrent": bool(done.get("concurrent")),
        }
    if name == "rewind_list":
        limit = max(1, min(50, int(args.get("limit", 10))))
        records = project.records()
        if args.get("agent"):
            records = [rec for rec in records if rec.get("agent") == args["agent"]]
        if args.get("session"):
            records = [rec for rec in records if rec.get("session") == args["session"]]
        records = records[:limit]
        return [
            {
                "id": rec["id"],
                "created": rec["created"],
                "state": rec["state"],
                "agent": rec.get("agent"),
                "session": rec.get("session"),
                "step": rec.get("step"),
                "call_id": rec.get("call_id"),
                "batch": rec.get("batch"),
                "concurrent": bool(rec.get("concurrent")),
                "label": rec["label"],
                "changes": list(rec.get("changes", {})),
                "summary": summary(rec),
            }
            for rec in records
        ]
    if name == "rewind_diff":
        record = project.get(args["id"])
        changes = get_changes(record)
        files = args.get("files")
        if files is not None and len(files) == 0:
            return {"summary": summary(record), "diffs": [], "skipped": record.get("skipped", {})}
        paths = select_paths(changes, files)
        diffs = ["\n".join(file_diff(project, path, *changes[path])) for path in paths]
        return {"summary": summary(record), "diffs": diffs, "skipped": record.get("skipped", {})}
    if name == "rewind_undo":
        record = project.get(args["id"])
        changes = get_changes(record)
        files = args.get("files")
        if files is None:
            if not args.get("all"):
                raise RewindError("Specify non-empty 'files' or set 'all': true to restore all changed files.")
            paths = select_paths(changes, None)
        else:
            if not isinstance(files, list):
                raise RewindError("'files' must be a list of relative file paths.")
            paths = select_paths(changes, files)
        force = bool(args.get("force", False))
        preview = project.preview_undo(record, paths, force=force)
        if args.get("dry_run"):
            return {
                "dry_run": True,
                "id": record["id"],
                "paths": preview["paths"],
                "conflicts": preview["conflicts"],
            }
        if preview["conflicts"] and not force:
            raise RewindError(
                "Undo stopped before writing.\n"
                f"Target paths: {', '.join(preview['paths'])}\n"
                "Conflicts (re-run with force=true to overwrite):\n  "
                + "\n  ".join(preview["conflicts"])
            )
        with project.lock():
            restored = project.undo(record, paths, force=force)
        return {
            "undo_id": restored["id"],
            "summary": summary(restored),
            "restored": preview["paths"],
            "conflicts": preview["conflicts"],
        }
    raise RewindError(f"Unknown tool: {name}")


def handle_request(message, default_root=".", default_store=None, state=None):
    store = default_store or default_storage()
    state = state if state is not None else {}
    req_id = message.get("id")
    method = message.get("method", "")
    params = message.get("params") or {}

    if method == "initialize":
        client_info = params.get("clientInfo") or {}
        if isinstance(client_info, dict) and client_info.get("name"):
            state["agent"] = str(client_info["name"])
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name", "")
        try:
            payload = _call_tool(tool_name, params.get("arguments"), default_root, store, state=state)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                    "isError": False,
                },
            }
        except (RewindError, OSError, ValueError, KeyError) as error:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            }
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_message(stream):
    while True:
        line = stream.readline()
        if not line:
            return None, False
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                header = stream.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            return json.loads(body.decode("utf-8")), True
        return json.loads(stripped.decode("utf-8")), False


def _write_message(stream, payload, framed):
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if framed:
        stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
    else:
        stream.write(encoded + b"\n")
    stream.flush()


def serve(default_root=".", default_store=None):
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    session_state = {}
    while True:
        try:
            message, framed = _read_message(stdin)
        except (ValueError, UnicodeDecodeError):
            continue
        if message is None:
            break
        response = handle_request(
            message, default_root=default_root, default_store=default_store, state=session_state
        )
        if response is not None:
            _write_message(stdout, response, framed)
