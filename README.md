# term-rewind

> **Ctrl+Z for your terminal and AI coding agents.**  
> Command-scoped filesystem snapshots, colored diffs, and conflict-safe selective undo — with zero external dependencies.

[简体中文说明 (Windows 桌面版 / 浮窗指南)](README.zh-CN.md)

---

## Why `term-rewind`?

Ever ran a script, formatter, or AI agent command without committing first—only to watch it overwrite configs, delete an uncommitted draft, or scatter broken files across your repo?

- `git checkout` cannot save uncommitted files you forgot to stage.
- `trash-cli` only catches `rm`, not scripts that overwrite or corrupt existing files in place.
- `LD_PRELOAD`-based undo tools break on macOS (SIP) and don't exist on Windows.

`term-rewind` takes fast, content-addressed incremental snapshots of your project directory before and after any command or AI agent task, lets you **inspect the exact diff in a keyboard TUI**, and **selectively undo** individual files without clobbering later edits.

---

## Key Features

- **Zero-dependency & Cross-platform**: Pure Python 3.10+ standard library CLI and terminal TUI (`termios` on macOS/Linux, Virtual Terminal on Windows).
- **Fast Incremental Snapshots**: Content-addressed SHA-256 object store with a stat cache (`mtime_ns`, `ctime_ns`, `size`, racy-write guard) so unchanged files aren't re-hashed.
- **Selective & Conflict-Safe Undo**: Roll back one file or an entire command. Detects if a file was edited afterwards and stops before overwriting (override with `--force`).
- **AI Agent & Shell Hooks + Built-in MCP Server**: Built-in `rewind begin` / `rewind end` task boundaries, ready-to-paste `rewind hook` snippets for **Claude Code**, **zsh**, **bash**, and **PowerShell**, plus a zero-dependency stdio MCP server (`rewind mcp`).
- **Optional Windows Desktop App & Floating Capsule**: Includes a zero-dependency Tkinter GUI (`app.pyw`) and a dockable floating capsule (`floating.py`) for Windows users.

---

## Quick Start

### Install the CLI

```bash
# Using uv or pipx (recommended)
uv tool install .
# or
pipx install .
# or editable install for development
pip install -e .
```

*(On Windows without installing Python globally, you can also run `.\rewind.ps1` or double-click `demo.cmd` / `term-rewind.exe`.)*

### 1. Wrap a risky command

```bash
rewind run -- python clean_data.py
```

### 2. Inspect history & selectively undo in the TUI

```bash
rewind
```

- `Up` / `Down` + `Enter` to open a command and inspect colorized diffs
- `U` to undo the selected file, `A` to undo all files in that record
- `Y` to confirm (or `F` to force-overwrite if a conflict is detected)

### 3. CLI workflow (scriptable)

```bash
rewind list
rewind diff                      # diffs the latest completed record (or pass an ID prefix)
rewind undo --yes                # undoes the latest completed record
rewind undo <id-prefix> --file draft.md
rewind undo <id-prefix> --file config.json --force --yes
rewind list --json
rewind status
rewind gc --keep 20
```

---

## Auto-Recording for AI Agents, MCP & Shells

Use `rewind hook` to print ready-to-use integration snippets:

```bash
rewind hook mcp             # Zero-dependency stdio MCP server config (Claude Code, Cursor, Antigravity)
rewind hook claude          # PreToolUse / PostToolUse / PostToolUseFailure hooks (pairs tool calls via stdin tool_use_id)
rewind hook claude --strict # Strict blocking mode (exits 2 on capture failure so Claude won't edit unprotected)
rewind hook zsh             # preexec / precmd auto-snapshot hook for zsh
rewind hook bash            # DEBUG trap / PROMPT_COMMAND hook for bash
rewind hook powershell      # `rw` command wrapper for PowerShell $PROFILE
```

Start the MCP server directly (exposes `rewind_begin`, `rewind_step`, `rewind_end`, `rewind_list`, `rewind_diff`, `rewind_undo`):

- `rewind_begin` / `rewind_end` accept optional `call_id` to pair overlapping tool calls cleanly (overlapping same-workspace runs share a `batch` ID).
- `rewind_undo` requires a non-empty `files` array or explicit `all: true`, and supports `dry_run: true` to inspect target paths and conflicts beforehand.

```bash
rewind mcp --root .
```

Or track multi-step agent checkpoints with per-agent attribution:

```bash
rewind begin "step 1: inspect auth" --agent claude --session s1
# ... step 1 edits ...
rewind step "step 2: fix token expiry"
# ... step 2 edits ...
rewind end --session s1
rewind list --agent claude
```

---

## Ignore Rules & Safety Model

- **Snapshot storage**: Stored outside your project in `%LOCALAPPDATA%\term-rewind` (Windows) or `~/.local/state/term-rewind` (`$XDG_STATE_HOME` on Linux/macOS), overridable via `--store`.
- **Default exclusions**: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.rewind`, `.env`, `.env.*`, symlinks/reparse points, and files over 8 MiB.
- **Custom exclusions**: Drop a `.rewindignore` file in your project root (supports glob patterns like `*.log` or `dist`).
- **Write-safe & recoverable**: Individual file writes use `fsync` + `os.replace`, path segments are re-checked against symlink escapes before reading or restoring, and interrupted multi-file undos save a `partial` record for reconciliation via `rewind end <id>`. Multi-file undo is **not** a full filesystem transaction, and excluded paths (`.env*`, symlinks, >8 MiB files) are not backed up — use `term-rewind` alongside Git and proper backups, not as a replacement.
- **No silent hook failures**: Hook snippets use `--warn-only` (printing a stderr warning and recording a `failed` history entry if capture fails) or `--strict` (exiting with code `2` to block unprotected agent edits).
- **Limits**: 5,000 files / 128 MiB per project snapshot, 8 MiB per file. Manual cleanup via `rewind gc --keep 20` removes older records while keeping the latest 20.

---

## Running Tests

```bash
python -m unittest discover -s tests -v
```
