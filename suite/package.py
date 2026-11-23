"""Series package: schema, validation, import and export (spec §4, §13).

A package is what a Claude chat produces when shaping a series. It is pushed
through the reeld MCP and stored as DB rows: a Series, its Episodes, and
versioned Documents (bible, skills, continuity, learnings).
"""

from __future__ import annotations

import re
import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import documents
from .models import (
    DocKind,
    Episode,
    EpisodeState,
    ScopeType,
    Series,
    SeriesStatus,
    SlotKind,
    VersionSource,
)

REQUIRED_SKILLS = ("script", "rubric", "visuals")
BIBLE_WORD_LIMIT = 600  # "under one page"
RUBRIC_MIN, RUBRIC_MAX = 6, 12
VAGUE_RUBRIC_WORDS = ("engaging", "interesting", "good", "nice", "compelling", "great", "catchy")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEYWORD_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CRITERION_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+)$", re.MULTILINE)


# ─── Schema ───────────────────────────────────────────────────────────────────


class Slot(BaseModel):
    """When an episode publishes. Precise dates feed Today/Tomorrow; vague ones don't."""

    kind: Literal["precise", "week", "month", "after", "unscheduled"] = "unscheduled"
    date: Optional[dt.date] = Field(
        None, description="precise: the day; week/month: any day inside that week or month"
    )
    time: Optional[dt.time] = Field(None, description="precise only, local time, optional")
    after_episode: Optional[int] = Field(None, description="kind=after: publish after this episode")


class PlanLine(BaseModel):
    number: int = Field(..., ge=1)
    title: str
    core_idea: str
    hook: str
    keyword: str = Field(..., description="comment keyword, one word, unique within the series")
    slot: Slot = Field(default_factory=Slot)
    overrides: dict[str, str] = Field(
        default_factory=dict,
        description="one-off profile overrides keyed by section heading, e.g. {'Length': '90 s'}",
    )


class AutomationSpec(BaseModel):
    name: str = Field(..., description="matches a skill named auto-<name>, e.g. 'blog' → auto-blog")
    order: int = 1
    enabled: bool = True
    config: dict = Field(default_factory=dict, description="targets etc., e.g. {'repo': '...'}")

    @field_validator("name")
    @classmethod
    def _slugish(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG_RE.match(v):
            raise ValueError("automation name must be lowercase-hyphenated, e.g. 'case-study-pdf'")
        return v


class SeriesPackage(BaseModel):
    slug: str = Field(..., description="lowercase-hyphenated id, e.g. 'ballot-math'")
    name: str
    status: Literal["draft", "active", "paused", "archived"] = "draft"
    bible: str = Field(
        ..., description="markdown, `## Section` headings; only what differs from the user profile"
    )
    plan: list[PlanLine]
    skills: dict[str, str] = Field(
        ...,
        description="markdown per skill: script, rubric, visuals, plus auto-<name> per automation",
    )
    automations: list[AutomationSpec] = Field(default_factory=list)
    continuity: str = Field("", description="empty for a new series; `## Ep N` entries")
    learnings: str = Field("", description="empty, or only rules the user stated")


class Issue(BaseModel):
    severity: Literal["error", "warning"]
    where: str
    message: str


# ─── Validation ───────────────────────────────────────────────────────────────


def _criteria(rubric_md: str) -> list[str]:
    return [m.group(1).strip() for m in _CRITERION_RE.finditer(rubric_md or "")]


def validate_package(pkg: SeriesPackage) -> list[Issue]:
    issues: list[Issue] = []

    def err(where: str, msg: str) -> None:
        issues.append(Issue(severity="error", where=where, message=msg))

    def warn(where: str, msg: str) -> None:
        issues.append(Issue(severity="warning", where=where, message=msg))

    if not _SLUG_RE.match(pkg.slug):
        err("slug", "use lowercase letters, digits and hyphens, e.g. 'ballot-math'")

    # Bible
    words = len(pkg.bible.split())
    if not pkg.bible.strip():
        err("bible", "bible is empty")
    elif words > BIBLE_WORD_LIMIT:
        warn("bible", f"{words} words; keep it under ~{BIBLE_WORD_LIMIT} (one page)")

    # Skills
    for key in REQUIRED_SKILLS:
        if not pkg.skills.get(key, "").strip():
            err(f"skills.{key}", "required skill is missing or empty")
    names = {a.name for a in pkg.automations}
    for a in pkg.automations:
        if not pkg.skills.get(f"auto-{a.name}", "").strip():
            err(f"automations.{a.name}", f"no skill 'auto-{a.name}' for this automation")
    for key in pkg.skills:
        if key.startswith("auto-") and key[5:] not in names:
            warn(f"skills.{key}", "automation skill not listed in automations; it will never run")
        elif not key.startswith("auto-") and key not in REQUIRED_SKILLS:
            warn(f"skills.{key}", "unknown skill name; no stage will load it")

    # Rubric
    criteria = _criteria(pkg.skills.get("rubric", ""))
    if pkg.skills.get("rubric", "").strip():
        if not RUBRIC_MIN <= len(criteria) <= RUBRIC_MAX:
            warn(
                "skills.rubric",
                f"{len(criteria)} criteria found; aim for {RUBRIC_MIN}–{RUBRIC_MAX} checkable items",
            )
        for c in criteria:
            lowered = c.lower()
            vague = [w for w in VAGUE_RUBRIC_WORDS if re.search(rf"\b{w}\b", lowered)]
            if vague:
                warn("skills.rubric", f"'{c[:60]}' uses vague wording ({', '.join(vague)}); make it pass/fail")

    # Plan
    if not pkg.plan:
        err("plan", "plan has no episodes")
    numbers = [p.number for p in pkg.plan]
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    if dupes:
        err("plan", f"duplicate episode numbers: {dupes}")
    if numbers and not dupes and sorted(numbers) != list(range(1, len(numbers) + 1)):
        warn("plan", "episode numbers are not consecutive from 1")
    seen_kw: dict[str, int] = {}
    for p in pkg.plan:
        where = f"plan[{p.number}]"
        for field in ("title", "core_idea", "hook", "keyword"):
            if not getattr(p, field).strip():
                err(where, f"{field} is empty")
        kw = p.keyword.strip()
        if kw and not _KEYWORD_RE.match(kw):
            warn(where, f"keyword '{kw}' should be one word (letters, digits, underscore)")
        if kw:
            prev = seen_kw.get(kw.lower())
            if prev is not None:
                err(where, f"keyword '{kw}' already used by Ep {prev}")
            seen_kw[kw.lower()] = p.number
        s = p.slot
        if s.kind in ("precise", "week", "month") and s.date is None:
            err(where, f"slot kind '{s.kind}' needs a date")
        if s.kind != "precise" and s.time is not None:
            warn(where, "slot time is only used for precise slots")
        if s.kind == "after":
            if s.after_episode is None:
                err(where, "slot kind 'after' needs after_episode")
            elif s.after_episode not in numbers or s.after_episode >= p.number:
                err(where, f"after_episode {s.after_episode} must be an earlier episode in the plan")
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


# ─── Import / export ──────────────────────────────────────────────────────────


class PackageError(Exception):
    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.issues = issues or []


def get_series(session: Session, user_id: int, slug: str) -> Optional[Series]:
    return session.scalar(select(Series).where(Series.user_id == user_id, Series.slug == slug))


def _apply_episode(ep: Episode, line: PlanLine) -> None:
    ep.title = line.title.strip()
    ep.core_idea = line.core_idea.strip()
    ep.hook = line.hook.strip()
    ep.keyword = line.keyword.strip()
    ep.slot_kind = SlotKind(line.slot.kind)
    ep.slot_date = line.slot.date
    ep.slot_time = line.slot.time
    ep.after_episode = line.slot.after_episode
    ep.overrides = dict(line.overrides)


def save_package(
    session: Session,
    user_id: int,
    pkg: SeriesPackage,
    *,
    mode: Literal["create", "update"],
    source: VersionSource = VersionSource.MCP,
) -> dict:
    """Create or update a series from a package. Raises PackageError on errors.

    Update rules: documents get a new version only when their text changed;
    episodes are matched by number; an episode missing from the package is
    removed only while still `planned`, otherwise kept and reported.
    """
    issues = validate_package(pkg)
    if has_errors(issues):
        raise PackageError("package has validation errors", issues)

    series = get_series(session, user_id, pkg.slug)
    if mode == "create" and series is not None:
        raise PackageError(f"series '{pkg.slug}' already exists; use update_series")
    if mode == "update" and series is None:
        raise PackageError(f"series '{pkg.slug}' not found; use create_series")

    if series is None:
        series = Series(user_id=user_id, slug=pkg.slug, name=pkg.name)
        session.add(series)
    series.name = pkg.name
    series.status = SeriesStatus(pkg.status)
    series.automations = [a.model_dump() for a in sorted(pkg.automations, key=lambda a: a.order)]
    session.flush()

    changed_docs: list[str] = []

    def put(kind: DocKind, key: str, content: str) -> None:
        ref, changed = documents.write(
            session,
            user_id=user_id,
            scope_type=ScopeType.SERIES,
            scope_id=series.id,
            kind=kind,
            key=key,
            content=content,
            source=source,
        )
        if changed:
            changed_docs.append(f"{kind.value}:{key} v{ref.version}")

    put(DocKind.BIBLE, "main", pkg.bible)
    for key, content in sorted(pkg.skills.items()):
        put(DocKind.SKILL, key, content)
    # Continuity and learnings grow from the pipeline; a package only seeds them,
    # so an update never wipes accumulated entries with an empty field.
    for kind, content in ((DocKind.CONTINUITY, pkg.continuity), (DocKind.LEARNINGS, pkg.learnings)):
        if content.strip() or documents.read(session, ScopeType.SERIES, series.id, kind) is None:
            put(kind, "main", content)

    # Episodes. Keywords are unique per series, so park existing ones on a
    # placeholder first to allow swaps between episodes within one update.
    existing = {e.number: e for e in series.episodes}
    for e in existing.values():
        e.keyword = f"__tmp_{e.number}"
    session.flush()

    created, updated, removed, kept = [], [], [], []
    wanted = {line.number for line in pkg.plan}
    for line in sorted(pkg.plan, key=lambda l: l.number):
        ep = existing.get(line.number)
        if ep is None:
            ep = Episode(series_id=series.id, number=line.number, state=EpisodeState.PLANNED)
            session.add(ep)
            created.append(line.number)
        else:
            updated.append(line.number)
        _apply_episode(ep, line)
    for num, ep in existing.items():
        if num in wanted:
            continue
        if ep.state == EpisodeState.PLANNED:
            session.delete(ep)
            removed.append(num)
        else:
            ep.keyword = f"orphan{num}"
            kept.append(num)
    session.flush()
    session.refresh(series)

    return {
        "series_id": series.id,
        "slug": series.slug,
        "mode": mode,
        "episodes": {"created": created, "updated": updated, "removed": removed},
        "kept_in_progress": kept,
        "documents_changed": changed_docs,
        "warnings": [i.model_dump() for i in issues if i.severity == "warning"]
        + (
            [
                {
                    "severity": "warning",
                    "where": "plan",
                    "message": f"episodes {kept} are past planning, so they were kept "
                    "with keyword 'orphan<N>'; fix their plan lines and update again",
                }
            ]
            if kept
            else []
        ),
    }


def export_package(session: Session, series: Series) -> SeriesPackage:
    """Current stored state as a package (the starting point for edits)."""

    def text(kind: DocKind, key: str = "main") -> str:
        ref = documents.read(session, ScopeType.SERIES, series.id, kind, key)
        return ref.content if ref else ""

    skills = {
        d.key: (documents.current(session, d).content if documents.current(session, d) else "")
        for d in documents.list_docs(session, ScopeType.SERIES, series.id, DocKind.SKILL)
    }
    plan = [
        PlanLine(
            number=e.number,
            title=e.title,
            core_idea=e.core_idea,
            hook=e.hook,
            keyword=e.keyword,
            slot=Slot(
                kind=e.slot_kind.value,
                date=e.slot_date,
                time=e.slot_time,
                after_episode=e.after_episode,
            ),
            overrides=e.overrides or {},
        )
        for e in series.episodes
    ]
    return SeriesPackage(
        slug=series.slug,
        name=series.name,
        status=series.status.value,
        bible=text(DocKind.BIBLE),
        plan=plan,
        skills=skills,
        automations=[AutomationSpec(**a) for a in (series.automations or [])],
        continuity=text(DocKind.CONTINUITY),
        learnings=text(DocKind.LEARNINGS),
    )
