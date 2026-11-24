"""Connecting Claude to reeld: MCP commands, connection checks, skills.

Everything here is computed for the machine the app runs on (paths, Python,
database), so the commands in Settings can be pasted as-is.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".claude" / "skills"


def _env(user_email: Optional[str]) -> dict[str, str]:
    env = {"PYTHONPATH": str(ROOT)}
    if os.environ.get("DATABASE_URL"):
        env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    email = os.environ.get("REELD_USER_EMAIL") or user_email
    if email:
        env["REELD_USER_EMAIL"] = email
    return env


def mcp_commands(user_email: Optional[str]) -> dict[str, Any]:
    env = _env(user_email)
    py = sys.executable
    env_flags = " ".join(f"-e {k}={shlex.quote(v)}" for k, v in env.items())
    return {
        "claude_code": f"claude mcp add --scope user reeld {env_flags} -- {shlex.quote(py)} -m suite.mcp_server",
        "desktop_json": {"mcpServers": {"reeld": {"command": py, "args": ["-m", "suite.mcp_server"], "env": env}}},
        "desktop_config_path": "~/Library/Application Support/Claude/claude_desktop_config.json",
        "python": py,
        "root": str(ROOT),
    }


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def skills() -> list[dict[str, str]]:
    out = []
    for d in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta = _frontmatter(d.read_text(encoding="utf-8"))
        name = d.parent.name
        out.append({
            "name": name,
            "description": meta.get("description", ""),
            "download": f"/api/suite/skills/{name}.zip",
            "install": f"mkdir -p ~/.claude/skills && cp -R {shlex.quote(str(d.parent))} ~/.claude/skills/",
        })
    return out


def skill_zip(name: str) -> Optional[bytes]:
    folder = SKILLS_DIR / name
    if not re.fullmatch(r"[a-z0-9-]+", name) or not (folder / "SKILL.md").exists():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                z.write(f, f"{name}/{f.relative_to(folder)}")
    return buf.getvalue()


def _run(args: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return -1, str(exc)


def status() -> dict[str, Any]:
    """Is Claude Code installed, and are reeld / SuperProfile registered?"""
    binary = os.environ.get("REELD_CLAUDE_BIN", "claude")
    if not shutil.which(binary):
        return {"claude": {"installed": False}, "reeld": None, "superprofile": None}
    sp = os.environ.get("REELD_SUPERPROFILE_SERVER", "superprofile")
    with ThreadPoolExecutor(3) as pool:
        ver, reeld, spr = pool.map(lambda a: _run(a), [[binary, "--version"], [binary, "mcp", "get", "reeld"],
                                                         [binary, "mcp", "get", sp]])

    def server(res: tuple[int, str]) -> dict[str, Any]:
        code, text = res
        low = text.lower()
        registered = code == 0 and "not found" not in low and "no mcp server" not in low
        m = re.search(r"status:\s*(?:\S+\s+)?(.+)", text, re.IGNORECASE)
        state = m.group(1).strip() if m else ""
        scope = (re.search(r"scope:\s*(.+)", text, re.IGNORECASE) or [None, ""])[1].strip() if registered else ""
        sl = state.lower()
        connected = registered and sl.startswith("connected")
        needs_auth = registered and ("auth" in sl or "sign" in sl)
        pending = registered and "approval" in sl
        return {"registered": registered, "connected": connected, "needs_auth": needs_auth, "pending": pending,
                "state": state, "scope": scope, "detail": text[:600]}

    return {"claude": {"installed": True, "version": ver[1].splitlines()[0] if ver[1] else ""},
            "reeld": server(reeld), "superprofile": server(spr)}
