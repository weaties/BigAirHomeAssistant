#!/usr/bin/env python3
"""PreToolUse hook: block edits to tracked files in the primary checkout.

CLAUDE.md requires all edits to happen in a linked worktree under
`.claude/worktrees/`. This hook enforces it for Edit / Write / NotebookEdit.

Allowed: linked worktrees, gitignored paths (secrets, data/), files outside
this repository. Blocked: anything else inside the primary checkout.

Exit 0 = allow, exit 2 = block (stderr is shown to the agent). Any internal
error allows the edit — a broken hook must never stop all editing.
Override for a deliberate primary-checkout edit: BIGAIR_ALLOW_PRIMARY_EDITS=1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def existing_parent(path: Path) -> Path | None:
    return next((p for p in path.parents if p.is_dir()), None)


def is_blocked(target: Path) -> str | None:
    folder = existing_parent(target)
    if folder is None:
        return None
    git_dir = git(folder, "rev-parse", "--absolute-git-dir")
    common = git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir is None or common is None or Path(git_dir) != Path(common):
        return None  # not a repo, or a linked worktree
    ours = git(
        Path(__file__).resolve().parent, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if ours is None or Path(ours) != Path(common):
        return None  # some other repository
    top = git(folder, "rev-parse", "--show-toplevel")
    if top is None or git(Path(top), "check-ignore", "-q", "--", str(target)) is not None:
        return None  # gitignored (check-ignore exits 0 → returns "")
    return (
        f"Blocked: {target} is a tracked path in the primary checkout ({top}).\n"
        "CLAUDE.md: always work in a git worktree. Use EnterWorktree, or\n"
        "`git worktree add .claude/worktrees/<name> -b <branch>`, and edit there.\n"
        "Deliberate exception: BIGAIR_ALLOW_PRIMARY_EDITS=1."
    )


def main() -> int:
    if os.environ.get("BIGAIR_ALLOW_PRIMARY_EDITS") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return 0
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(payload.get("cwd") or Path.cwd()) / path
        reason = is_blocked(path)
    except Exception:  # noqa: BLE001 — fail open
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
