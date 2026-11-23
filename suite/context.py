"""Context resolver (spec §3 and §4).

Builds the compact context bundle a single pipeline stage sends to Claude:

1. The *profile*: the user voice profile, overridden section by section by the
   series bible, then by the episode's own overrides. Sections are markdown
   `## Heading` blocks; a lower layer replaces a same-named section (case
   insensitive) and can add new ones. One merged block, so nothing is sent twice.
2. Only the extra parts that stage needs (its skill, this episode's plan line,
   recent continuity, learnings, the research note or script).

Every bundle carries a manifest (which document versions went in, and a
token estimate) that the runner stores on the StageRun.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import documents
from .models import DocKind, Episode, ScopeType, Series

CONTINUITY_WINDOW = 5  # most recent finished episodes the script stage sees
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_EP_ENTRY_RE = re.compile(r"^##\s+Ep(?:isode)?\s*(\d+)\b.*$", re.MULTILINE | re.IGNORECASE)


# ─── Markdown section merging ─────────────────────────────────────────────────


def split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Split markdown into (preamble, [(heading, body), ...]) on `## ` headings."""
    md = md or ""
    matches = list(_SECTION_RE.finditer(md))
    if not matches:
        return md.strip(), []
    preamble = md[: matches[0].start()].strip()
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections.append((m.group(1).strip(), md[m.end() : end].strip()))
    return preamble, sections


def merge_sections(base: str, *overrides: str | dict[str, str]) -> str:
    """Overlay markdown (or {heading: body} dicts) onto base, section by section.

    A same-named section is replaced in place; new sections are appended in the
    order they appear. A non-empty override preamble replaces the base preamble.
    """
    preamble, sections = split_sections(base)
    order = [h for h, _ in sections]
    bodies = {h.lower(): (h, b) for h, b in sections}

    for layer in overrides:
        if not layer:
            continue
        if isinstance(layer, dict):
            layer_pre, layer_secs = "", [(k, v) for k, v in layer.items()]
        else:
            layer_pre, layer_secs = split_sections(layer)
        if layer_pre:
            preamble = layer_pre
        for heading, body in layer_secs:
            key = heading.lower()
            if key in bodies:
                original = bodies[key][0]
                bodies[key] = (original, body.strip())
            else:
                order.append(heading)
                bodies[key] = (heading, body.strip())

    out = [preamble] if preamble else []
    for heading in order:
        h, body = bodies[heading.lower()]
        out.append(f"## {h}\n\n{body}".rstrip())
    return "\n\n".join(out).strip() + "\n"


def continuity_window(continuity_md: str, before_episode: int, n: int = CONTINUITY_WINDOW) -> str:
    """Last `n` continuity entries (`## Ep N ...`) for episodes before this one."""
    matches = list(_EP_ENTRY_RE.finditer(continuity_md or ""))
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(continuity_md)
        num = int(m.group(1))
        if num < before_episode:
            entries.append((num, continuity_md[m.start() : end].strip()))
    entries.sort(key=lambda e: e[0])
    return "\n\n".join(text for _, text in entries[-n:])


# ─── Stage recipes ────────────────────────────────────────────────────────────

# Which parts each stage loads, beyond the merged profile and the episode line.
# Parts: neighbours, continuity, learnings, research, script, scene_plan,
#        skill:<key> (skill:auto is resolved per automation name).
STAGE_RECIPES: dict[str, list[str]] = {
    "research": ["skill:script", "continuity"],
    "script": ["skill:script", "neighbours", "continuity", "learnings", "research"],
    "critic": ["skill:rubric", "learnings", "script"],
    "visuals": ["skill:visuals", "script"],
    "automation": ["skill:auto", "research", "script"],
}


def estimate_tokens(text: str) -> int:
    # ~4 characters per token is close enough for budgeting and logs.
    return max(1, len(text) // 4) if text else 0


@dataclass
class ContextPart:
    name: str
    text: str
    source: dict[str, Any] = field(default_factory=dict)  # doc id/version etc.


@dataclass
class ResolvedContext:
    stage: str
    series_slug: str
    episode_number: int
    parts: list[ContextPart]
    skill: ContextPart | None
    missing: list[str]

    def render(self) -> str:
        """The `context.md` body: everything except the stage skill."""
        blocks = [f"# Context: {self.series_slug} Ep {self.episode_number} ({self.stage})"]
        for p in self.parts:
            blocks.append(f"<{p.name}>\n{p.text.strip()}\n</{p.name}>")
        return "\n\n".join(blocks) + "\n"

    def manifest(self) -> dict[str, Any]:
        body = self.render()
        skill_text = self.skill.text if self.skill else ""
        return {
            "stage": self.stage,
            "series": self.series_slug,
            "episode": self.episode_number,
            "parts": [{"name": p.name, **p.source, "tokens": estimate_tokens(p.text)} for p in self.parts],
            "skill": ({"name": self.skill.name, **self.skill.source} if self.skill else None),
            "missing": self.missing,
            "tokens_estimate": estimate_tokens(body) + estimate_tokens(skill_text),
        }

    def materialise(self, workdir: str | Path) -> dict[str, Path]:
        """Write the throwaway files a `claude -p` run reads (spec §9).

        The caller owns `workdir` and deletes it after ingesting the outputs.
        """
        root = Path(workdir)
        root.mkdir(parents=True, exist_ok=True)
        paths = {"context": root / "context.md"}
        paths["context"].write_text(self.render(), encoding="utf-8")
        if self.skill:
            paths["skill"] = root / "SKILL.md"
            paths["skill"].write_text(self.skill.text, encoding="utf-8")
        return paths


# ─── Resolver ─────────────────────────────────────────────────────────────────


def episode_line(ep: Episode) -> str:
    slot = ep.slot_kind.value
    if ep.slot_date:
        slot += f" {ep.slot_date.isoformat()}"
        if ep.slot_time:
            slot += f" {ep.slot_time.strftime('%H:%M')}"
    if ep.after_episode:
        slot += f" (after Ep {ep.after_episode})"
    return (
        f"Episode {ep.number}: {ep.title}\n"
        f"- Core idea: {ep.core_idea}\n"
        f"- Hook: {ep.hook}\n"
        f"- Comment keyword: {ep.keyword}\n"
        f"- Slot: {slot}"
    )


def _doc_part(name: str, ref: documents.DocRef | None) -> ContextPart | None:
    if ref is None or not ref.content.strip():
        return None
    return ContextPart(name, ref.content, {"document_id": ref.document_id, "version": ref.version})


def resolve(
    session: Session,
    episode: Episode,
    stage: str,
    *,
    automation: str | None = None,
    draft: str | None = None,
) -> ResolvedContext:
    """Build the context bundle for one stage of one episode.

    `draft` lets the critic loop pass a not-yet-saved script in place of the
    stored one. `automation` names the automation for the "automation" stage.
    """
    if stage not in STAGE_RECIPES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(STAGE_RECIPES)}")
    if stage == "automation" and not automation:
        raise ValueError("the automation stage needs an automation name")

    series: Series = episode.series
    user_id = series.user_id
    parts: list[ContextPart] = []
    missing: list[str] = []

    # 1. Merged profile: user voice ← series bible ← episode overrides.
    voice = documents.read(session, ScopeType.USER, user_id, DocKind.VOICE)
    bible = documents.read(session, ScopeType.SERIES, series.id, DocKind.BIBLE)
    if bible is None:
        missing.append("bible")
    profile = merge_sections(
        voice.content if voice else "",
        bible.content if bible else "",
        episode.overrides or {},
    )
    parts.append(
        ContextPart(
            "profile",
            profile,
            {
                "voice_version": voice.version if voice else None,
                "bible_version": bible.version if bible else None,
                "episode_overrides": sorted((episode.overrides or {}).keys()),
            },
        )
    )
    parts.append(ContextPart("episode", episode_line(episode), {"episode_id": episode.id}))

    # 2. Stage-specific parts.
    skill: ContextPart | None = None
    for item in STAGE_RECIPES[stage]:
        if item.startswith("skill:"):
            key = item.split(":", 1)[1]
            if key == "auto":
                key = f"auto-{automation}"
            ref = documents.read(session, ScopeType.SERIES, series.id, DocKind.SKILL, key)
            skill = _doc_part(f"skill:{key}", ref)
            if skill is None:
                missing.append(f"skill:{key}")
        elif item == "neighbours":
            near = [e for e in series.episodes if abs(e.number - episode.number) == 1]
            if near:
                text = "\n".join(
                    f"- Ep {e.number} ({'previous' if e.number < episode.number else 'next'}): "
                    f"{e.title}: {e.core_idea}"
                    for e in sorted(near, key=lambda e: e.number)
                )
                parts.append(ContextPart("neighbours", text))
        elif item == "continuity":
            ref = documents.read(session, ScopeType.SERIES, series.id, DocKind.CONTINUITY)
            window = continuity_window(ref.content, episode.number) if ref else ""
            if window:
                parts.append(
                    ContextPart("continuity", window, {"document_id": ref.document_id, "version": ref.version})
                )
        elif item == "learnings":
            part = _doc_part(
                "learnings", documents.read(session, ScopeType.SERIES, series.id, DocKind.LEARNINGS)
            )
            if part:
                parts.append(part)
        elif item in ("research", "script", "scene_plan"):
            if item == "script" and draft is not None:
                parts.append(ContextPart("script", draft, {"draft": True}))
                continue
            kind = DocKind(item)
            part = _doc_part(item, documents.read(session, ScopeType.EPISODE, episode.id, kind))
            if part:
                parts.append(part)
            elif item == "script" or (item == "research" and stage == "automation"):
                missing.append(item)

    return ResolvedContext(
        stage=stage if not automation else f"automation:{automation}",
        series_slug=series.slug,
        episode_number=episode.number,
        parts=parts,
        skill=skill,
        missing=missing,
    )
