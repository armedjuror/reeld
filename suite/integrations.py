"""Outside services: settings storage, git publishing, SuperProfile, publishers."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Integration


def get(session: Session, user_id: int, provider: str) -> dict[str, Any]:
    row = session.scalar(select(Integration).where(Integration.user_id == user_id, Integration.provider == provider))
    return dict(row.data or {}) if row else {}


def put(session: Session, user_id: int, provider: str, data: dict[str, Any], merge: bool = True) -> dict[str, Any]:
    row = session.scalar(select(Integration).where(Integration.user_id == user_id, Integration.provider == provider))
    if row is None:
        row = Integration(user_id=user_id, provider=provider, data={})
        session.add(row)
    row.data = {**(row.data or {}), **data} if merge else dict(data)
    session.flush()
    return dict(row.data)


def public_view(data: dict[str, Any]) -> dict[str, Any]:
    """Settings safe to show in the UI (secrets masked)."""
    out = {}
    for k, v in data.items():
        if any(s in k for s in ("token", "secret", "password")) and isinstance(v, str) and v:
            out[k] = v[:4] + "…" + v[-4:] if len(v) > 12 else "•••"
        else:
            out[k] = v
    return out


# ─── SuperProfile ─────────────────────────────────────────────────────────────


def superprofile_server(session: Session, user_id: int) -> Optional[str]:
    """Name of the SuperProfile MCP server registered in Claude Code, if enabled.

    Setup (once, on the Mac): `claude mcp add --scope user --transport http
    superprofile <server url>`, then `/mcp` in Claude Code to sign in. Steps
    then reach it as tools `mcp__superprofile__*`.
    """
    cfg = get(session, user_id, "superprofile")
    if cfg.get("enabled") or os.environ.get("REELD_SUPERPROFILE") == "1":
        return cfg.get("server_name") or os.environ.get("REELD_SUPERPROFILE_SERVER", "superprofile")
    return None


# ─── Git publishing (blog case studies etc.) ──────────────────────────────────


def _fmt(template: str, values: dict[str, Any]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def git_publish(content: str, config: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Write `content` into a local git checkout, commit and (optionally) push.

    config: repo_path (required), path (template, e.g.
    "_posts/{date}-{series}-ep{number}.md"), branch, push (default true),
    url (template for the public URL), commit_message (template).
    Template values: series, number, keyword, title, slug, date.
    """
    repo = Path(os.path.expanduser(config["repo_path"]))
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git checkout")
    values = {"date": date.today().isoformat(), **values}
    rel = _fmt(config.get("path", "{series}/ep{number}-{slug}.md"), values)
    target = (repo / rel).resolve()
    if repo.resolve() not in target.parents:
        raise RuntimeError("path escapes the repository")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")

    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
        return proc.stdout.strip()

    if config.get("branch"):
        git("checkout", config["branch"])
    git("add", rel)
    status = git("status", "--porcelain", "--", rel)
    commit = None
    if status:
        git("commit", "-m", _fmt(config.get("commit_message", "Add {series} Ep {number}: {title}"), values))
        commit = git("rev-parse", "HEAD")
        if config.get("push", True):
            git("push")
    url = _fmt(config["url"], {**values, "path": rel}) if config.get("url") else None
    return {"path": rel, "commit": commit, "url": url, "pushed": bool(commit and config.get("push", True))}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "episode"
