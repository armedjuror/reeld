"""reeld MCP server (spec §4 and §13).

Lets a Claude chat or Claude Code session read the user's voice profile and
push a series package straight into the database. Run over stdio:

    python -m suite.mcp_server

Register it with Claude Code (see README, "reeld MCP"):

    claude mcp add reeld -- python -m suite.mcp_server
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select

from . import documents
from .db import session_scope
from .models import DocKind, Episode, ScopeType, Series, VersionSource
from .package import (
    PackageError,
    SeriesPackage,
    export_package,
    get_series as _get_series,
    has_errors,
    save_package,
    validate_package,
)
from .users import current_user_id

INSTRUCTIONS = """\
reeld stores reel series and the creator's voice profile. If the voice
profile is missing or thin (check_voice_profile), interview the creator
first: the reeld-voice-profile skill or the voice_interview prompt. Save
one section at a time with update_voice_section.
Workflow for a new series: call
get_user_profile first and record only differences from it in the bible;
draft the full package; call validate_series_package and fix every error;
show the user a short summary; call create_series only after they approve.
To change an existing series: get_series, edit the package, validate,
then update_series. Nothing is written to files; everything goes to the DB.
"""

server = MCPServer(name="reeld", instructions=INSTRUCTIONS, version="0.1.0")


def _failure(exc: PackageError) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "issues": [i.model_dump() for i in exc.issues]}


@server.tool()
def get_user_profile() -> dict[str, Any]:
    """Return the user's voice profile: narration style, tone, language habits,
    banned phrases and brand defaults, as markdown with `## Section` headings.
    Series bibles override it section by section, so a bible should contain
    only sections that differ from this profile."""
    with session_scope() as s:
        uid = current_user_id(s)
        ref = documents.read(s, ScopeType.USER, uid, DocKind.VOICE)
        return {
            "version": ref.version if ref else 0,
            "content": ref.content if ref else "",
            "note": None if ref else "No profile yet. Ask the user and save one with set_user_profile.",
        }


@server.tool()
def set_user_profile(content: str, note: str = "") -> dict[str, Any]:
    """Save a new version of the user's voice profile (full markdown, `## Section`
    headings such as Narration, Tone, Language, Banned phrases, Brand, CTA).
    Only call this when the user asks to change their profile."""
    with session_scope() as s:
        uid = current_user_id(s)
        ref, changed = documents.write(
            s,
            user_id=uid,
            scope_type=ScopeType.USER,
            scope_id=uid,
            kind=DocKind.VOICE,
            content=content,
            source=VersionSource.MCP,
            note=note or None,
        )
        return {"ok": True, "version": ref.version, "changed": changed}


@server.tool()
def get_voice_template() -> dict[str, Any]:
    """The voice profile structure: each section, its purpose, and the questions
    an interview must answer before the section is clear. Use it to plan the
    voice interview; don't save the template itself."""
    from .voice import SECTIONS, template

    return {
        "sections": [{"heading": s.heading, "purpose": s.purpose, "must_answer": list(s.must_answer)}
                     for s in SECTIONS],
        "skeleton": template(),
    }


@server.tool()
def check_voice_profile() -> dict[str, Any]:
    """How complete the saved voice profile is: each section is ok, thin or
    missing, with the questions still open. Use it to decide what to ask next."""
    from .voice import coverage

    with session_scope() as s:
        uid = current_user_id(s)
        ref = documents.read(s, ScopeType.USER, uid, DocKind.VOICE)
        return {"version": ref.version if ref else 0, **coverage(ref.content if ref else "")}


@server.tool()
def update_voice_section(heading: str, content: str) -> dict[str, Any]:
    """Save one section of the voice profile (replaces that `## heading`
    section, keeps the rest). Write in the creator's own words; include a
    short example line where it helps. Only save what the creator confirmed."""
    from .voice import coverage, update_section

    if not heading.strip() or not content.strip():
        return {"ok": False, "error": "heading and content are required"}
    with session_scope() as s:
        uid = current_user_id(s)
        cur = documents.read(s, ScopeType.USER, uid, DocKind.VOICE)
        new = update_section(cur.content if cur else "# Voice profile\n", heading, content)
        ref, changed = documents.write(s, user_id=uid, scope_type=ScopeType.USER, scope_id=uid,
                                       kind=DocKind.VOICE, content=new, source=VersionSource.MCP,
                                       note=f"section: {heading.strip()}")
        cov = coverage(new)
        return {"ok": True, "version": ref.version, "changed": changed,
                "progress": f"{cov['ok']}/{cov['total']} sections clear"}


@server.prompt()
def voice_interview() -> str:
    """Interview me until the tone of my reels is clear, then save my voice profile to reeld."""
    from pathlib import Path

    skill = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "reeld-voice-profile" / "SKILL.md"
    body = skill.read_text(encoding="utf-8") if skill.exists() else ""
    body = body.split("---", 2)[-1] if body.startswith("---") else body
    return "Run this voice interview with me now, following these instructions exactly.\n\n" + body


@server.tool()
def validate_series_package(package: SeriesPackage) -> dict[str, Any]:
    """Check a series package without saving it. Fix every `error` before
    create_series or update_series; `warning`s are advice."""
    issues = validate_package(package)
    return {"ok": not has_errors(issues), "issues": [i.model_dump() for i in issues]}


@server.tool()
def create_series(package: SeriesPackage) -> dict[str, Any]:
    """Create a new series from a complete package. Only call after the user
    has approved the summary. Fails if the slug already exists."""
    with session_scope() as s:
        try:
            result = save_package(s, current_user_id(s), package, mode="create")
        except PackageError as exc:
            return _failure(exc)
        return {"ok": True, **result}


@server.tool()
def update_series(package: SeriesPackage) -> dict[str, Any]:
    """Replace an existing series' package (matched by slug). Documents get a new
    version only when changed; episodes are matched by number; episodes already
    past planning are never deleted. Start from get_series to avoid losing data."""
    with session_scope() as s:
        try:
            result = save_package(s, current_user_id(s), package, mode="update")
        except PackageError as exc:
            return _failure(exc)
        return {"ok": True, **result}


@server.tool()
def get_series(slug: str) -> dict[str, Any]:
    """Return a series' current package plus each episode's pipeline state."""
    with session_scope() as s:
        series = _get_series(s, current_user_id(s), slug)
        if series is None:
            return {"ok": False, "error": f"series '{slug}' not found"}
        return {
            "ok": True,
            "package": export_package(s, series).model_dump(mode="json"),
            "episode_states": {str(e.number): e.state.value for e in series.episodes},
        }


@server.tool()
def list_series() -> dict[str, Any]:
    """List the user's series with status and episode counts."""
    with session_scope() as s:
        uid = current_user_id(s)
        rows = s.execute(
            select(Series.slug, Series.name, Series.status, func.count(Episode.id))
            .outerjoin(Episode, Episode.series_id == Series.id)
            .where(Series.user_id == uid)
            .group_by(Series.id)
            .order_by(Series.name)
        ).all()
        return {
            "series": [
                {"slug": slug, "name": name, "status": status.value, "episodes": n}
                for slug, name, status, n in rows
            ]
        }


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
