"""Episode pipeline (spec §5) with checkpointed, resumable steps (spec §10).

`advance(session, episode)` moves an episode forward until it reaches a gate
that needs the user, a usage-limit pause, or a failure:

    PLANNED → SCRIPTING (research, draft, critic ⇄ revise ×3)
            → SCRIPT_REVIEW            gate 1: approve_script()
            → AWAITING_RECORDING       attach_recording()
            → AUDIO_PROCESSING (take selection, cleanup, timings)
            → AUDIO_REVIEW             gate 2: approve_audio()
            → EDITING (scene plan + SVGs by Claude, ffmpeg render)
            → VIDEO_REVIEW             approve_video()
            → AUTOMATIONS (series automations, post copy, continuity)
            → PUBLISHING (SuperProfile via Claude, or a manual task)
            → LIVE

Each Claude call is one `StageRun` keyed by (episode, stage, step). A step
that succeeded is never rerun; a paused step resumes its Claude session.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audio as audio_mod
from . import documents, integrations, media, schedule
from .context import resolve
from .models import (
    DocKind,
    Episode,
    EpisodeState,
    MediaKind,
    RunStatus,
    ScopeType,
    Series,
    SeriesStatus,
    SlotKind,
    StageRun,
    TaskStatus,
    TaskType,
    VersionSource,
)
from .runner import Runner, RunnerError, StepRequest, UsageLimitError, extract_json, get_runner
from .video import Scene, render_reel

MAX_REVISIONS = int(os.environ.get("REELD_MAX_REVISIONS", "3"))
SCRIPTS_AHEAD = int(os.environ.get("REELD_SCRIPTS_AHEAD", "3"))
DISTIL_EVERY = int(os.environ.get("REELD_DISTIL_EVERY", "10"))


class StepPaused(Exception):
    def __init__(self, resume_at: datetime):
        super().__init__(f"paused until {resume_at.isoformat()}")
        self.resume_at = resume_at


class StepFailed(Exception):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─── Step execution ───────────────────────────────────────────────────────────


def claude_paused_until(session: Session) -> Optional[datetime]:
    """While any step is paused for a usage limit, no new Claude step starts."""
    runs = session.scalars(select(StageRun).where(StageRun.status == RunStatus.PAUSED))
    latest = max((_utc(r.resume_at) for r in runs if r.resume_at), default=None)
    return latest if latest and latest > now() else None


def latest_run(session: Session, episode_id: int, stage: str, step: str) -> Optional[StageRun]:
    return session.scalar(
        select(StageRun)
        .where(StageRun.episode_id == episode_id, StageRun.stage == stage, StageRun.step == step)
        .order_by(StageRun.id.desc())
        .limit(1)
    )


def run_step(
    session: Session,
    episode: Episode,
    stage: str,
    step: str,
    build: Callable[[], tuple[StepRequest, dict[str, Any]]],
    runner: Runner,
) -> StageRun:
    """Run one checkpointed Claude step. `build` returns (request, manifest).

    Returns the succeeded run. Raises StepPaused or StepFailed.
    """
    prev = latest_run(session, episode.id, stage, step)
    if prev and prev.status == RunStatus.SUCCEEDED:
        return prev
    paused = claude_paused_until(session)
    if paused:
        raise StepPaused(paused)

    req, manifest = build()
    resume_from = None
    attempt = 1
    if prev is not None:
        attempt = prev.attempt + (1 if prev.status == RunStatus.FAILED else 0)
        if prev.status in (RunStatus.PAUSED, RunStatus.RUNNING) and prev.claude_session_id:
            resume_from = prev.claude_session_id
    if resume_from:
        req.resume_session_id = resume_from
        req.prompt = (
            "You were interrupted. Continue this step from where you stopped. Re-read "
            "context.md if needed, then write the complete deliverable to "
            f"{req.output_file} in the current directory (the previous directory is gone)."
        )

    run = prev if prev is not None and prev.status in (RunStatus.PAUSED, RunStatus.RUNNING) else None
    if run is None:
        run = StageRun(episode_id=episode.id, stage=stage, step=step, attempt=attempt)
        session.add(run)
    run.status = RunStatus.RUNNING
    run.context_manifest = manifest
    run.started_at = now()
    run.error = None
    session.flush()
    session.commit()  # make "running" visible while Claude works

    try:
        result = runner.run(req)
    except UsageLimitError as exc:
        run.status = RunStatus.PAUSED
        run.resume_at = exc.resume_at
        run.claude_session_id = exc.session_id or run.claude_session_id
        run.error = str(exc)[:1000]
        session.commit()
        raise StepPaused(exc.resume_at) from exc
    except RunnerError as exc:
        run.status = RunStatus.FAILED
        run.claude_session_id = exc.session_id or run.claude_session_id
        run.error = str(exc)[:4000]
        run.finished_at = now()
        _fail(session, episode, run, str(exc))
        session.commit()
        raise StepFailed(str(exc)) from exc

    run.status = RunStatus.SUCCEEDED
    run.claude_session_id = result.session_id
    run.output_text = result.output
    run.cost_usd = result.cost_usd
    run.finished_at = now()
    run.resume_at = None
    if result.files:
        run.context_manifest = {**(run.context_manifest or {}), "produced_files": sorted(result.files)}
    session.flush()
    run._files = result.files  # transient: produced files for the caller
    return run


def _fail(session: Session, episode: Episode, run: Optional[StageRun], message: str) -> None:
    episode.meta = {**(episode.meta or {}), "error": message[:1000], "failed_step": run.step if run else None}
    schedule.open_task(
        session,
        user_id=episode.series.user_id,
        task_type=TaskType.FIX_FAILURE,
        episode=episode,
        payload={"summary": message[:300], "stage": run.stage if run else None, "step": run.step if run else None},
        stage_run_id=run.id if run else None,
    )


def _request(ctx, prompt: str, output_file: str = "output.md", **kw) -> tuple[StepRequest, dict]:
    files = {"context.md": ctx.render()}
    if ctx.skill:
        files["SKILL.md"] = ctx.skill.text
    return StepRequest(prompt=prompt, files=files, output_file=output_file, **kw), ctx.manifest()


def _save_doc(session: Session, episode: Episode, kind: DocKind, content: str, *, key: str = "main",
              source: VersionSource = VersionSource.PIPELINE, note: str | None = None):
    ref, _ = documents.write(
        session, user_id=episode.series.user_id, scope_type=ScopeType.EPISODE, scope_id=episode.id,
        kind=kind, key=key, content=content, source=source, note=note,
    )
    return ref


def _doc(session: Session, episode: Episode, kind: DocKind, key: str = "main"):
    return documents.read(session, ScopeType.EPISODE, episode.id, kind, key)


# ─── Prompts (series skills carry the specifics) ──────────────────────────────

P_RESEARCH = (
    "Write the research note for this episode, following SKILL.md and the profile in context.md. "
    "Concise and factual, at most about three pages of markdown, with a Sources list of URLs you "
    "actually used. It is also the source for the comment-keyword deliverable."
)
P_DRAFT = (
    "Write this episode's script following SKILL.md, the profile and the research note in context.md. "
    "Output only what will be spoken: one spoken line per line, in the language the profile sets. "
    "Optional visual cues go on their own line in [square brackets]. No headings, no commentary."
)
P_CRITIC = (
    "You are a strict reviewer. The <script> in context.md is a draft. Check it against every "
    "criterion in SKILL.md (the rubric) and the profile. Output JSON only, in output.json:\n"
    '{"pass": true|false, "results": [{"criterion": "...", "pass": true|false, '
    '"evidence": "quote or reason", "fix": "what to change, empty if pass"}], "summary": "one line"}\n'
    '"pass" is true only if every criterion passes.'
)
P_REVISE = (
    "Revise the <script> in context.md to fix every failed criterion in <critic>, keeping what "
    "already works. Follow SKILL.md and the profile. Output only the full revised script, same "
    "format: spoken lines, optional [visual cues] on their own lines."
)
P_REVISE_USER = (
    "Revise the <script> in context.md according to the creator's note in <feedback>. Keep "
    "everything the note doesn't touch. Output only the full revised script, same format."
)
P_VISUALS = (
    "Design the visuals for this reel following SKILL.md and the profile. <timings> gives each "
    "spoken line's start and end in seconds. Make one scene per idea, each shown 2–6 seconds.\n"
    "For each scene write a self-contained SVG file scenes/scene-01.svg, scenes/scene-02.svg, ... "
    'with viewBox="0 0 1080 1920" (portrait 9:16), no external images or fonts (system font '
    "families only), readable on a phone, safe margins of 80px, nothing important in the bottom "
    "320px (captions/UI). Then write scenes.json:\n"
    '[{"start": seconds, "file": "scenes/scene-01.svg", "description": "what it shows"}]\n'
    "Scene starts must increase and the first must be 0."
)
P_AUTOMATION = (
    "Run this automation for the episode exactly as SKILL.md describes, using the research note "
    "and script in context.md. Write the deliverable to output.md."
)
P_POST_COPY = (
    "Write the Instagram post copy for this reel from context.md. Output JSON only, in output.json:\n"
    '{"caption": "...", "hashtags": ["..."], "dm_message": "...", "first_comment": "..." or null}\n'
    "The caption ends with the call to action to comment the episode's comment keyword exactly as "
    "written in <episode>. dm_message is the reply people get after commenting: short, friendly, "
    "and it includes the deliverable link(s) from <links> if any. Match the profile's tone."
)
P_CONTINUITY = (
    "Write this episode's continuity entry for future episodes, from the script in context.md. "
    "Output markdown only, in exactly this shape (two to four bullets, short):\n"
    "## Ep {number}: {title}\n- Covered: ...\n- Promised: ... (or none)\n- Callbacks: ... (or none)"
)
P_PUBLISH = (
    "Publish this reel with the SuperProfile tools ({server}). The creator has already approved "
    "everything below, so publish rather than leaving drafts.\n"
    "1. Schedule an Instagram reel: video URL {video_url}, caption below, {when}.\n"
    "2. Create a comment-to-DM automation on that reel: keyword \"{keyword}\" (match the keyword, "
    "case-insensitive), reply with the DM message below.\n"
    "Then write output.json: {{\"scheduled\": true|false, \"post_id\": ..., \"automation_id\": ..., "
    "\"scheduled_for\": \"ISO time or null\", \"notes\": \"...\"}}\n\n"
    "CAPTION:\n{caption}\n\nDM MESSAGE:\n{dm}"
)


# ─── Stages ───────────────────────────────────────────────────────────────────


def _stage_script(session: Session, ep: Episode, runner: Runner) -> None:
    ep.state = EpisodeState.SCRIPTING
    run = run_step(session, ep, "script", "research",
                   lambda: _request(resolve(session, ep, "research"), P_RESEARCH, allowed_tools=None), runner)
    if _doc(session, ep, DocKind.RESEARCH) is None:
        _save_doc(session, ep, DocKind.RESEARCH, run.output_text or "")

    run = run_step(session, ep, "script", "draft", lambda: _request(resolve(session, ep, "script"), P_DRAFT), runner)
    draft = run.output_text or ""
    verdict: dict[str, Any] = {}
    for n in range(1, MAX_REVISIONS + 2):
        d = draft
        run = run_step(session, ep, "script", f"critic-{n}",
                       lambda: _request(resolve(session, ep, "critic", draft=d), P_CRITIC, output_file="output.json"),
                       runner)
        try:
            verdict = extract_json(run.output_text or "")
        except ValueError:
            verdict = {"pass": False, "summary": "critic output was not valid JSON", "results": []}
        if verdict.get("pass") or n > MAX_REVISIONS:
            break
        crit = json.dumps(verdict, ensure_ascii=False, indent=1)
        run = run_step(session, ep, "script", f"revise-{n}",
                       lambda: _request(resolve(session, ep, "revise", draft=d, extra=[("critic", crit)]), P_REVISE),
                       runner)
        draft = run.output_text or draft

    _save_doc(session, ep, DocKind.SCRIPT, draft, note="pipeline draft" + (" (passed critic)" if verdict.get("pass") else " (critic flags remain)"))
    _save_doc(session, ep, DocKind.CRITIC, json.dumps(verdict, ensure_ascii=False, indent=1))
    ep.state = EpisodeState.SCRIPT_REVIEW
    failed = [r.get("criterion") for r in verdict.get("results", []) if not r.get("pass")]
    schedule.open_task(session, user_id=ep.series.user_id, task_type=TaskType.APPROVE_SCRIPT, episode=ep,
                       payload={"summary": "Passed the rubric" if verdict.get("pass")
                                else f"{len(failed)} rubric flag(s) remain", "flags": failed})


def _stage_audio(session: Session, ep: Episode, scorer=None) -> None:
    raw = media.latest(session, ep.id, MediaKind.RAW_TAKE)
    script = _doc(session, ep, DocKind.SCRIPT)
    if raw is None or script is None:
        ep.state = EpisodeState.AWAITING_RECORDING
        return
    import tempfile

    with media.local_copy(raw) as raw_path, tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clean.m4a"
        try:
            result = audio_mod.process_take(Path(raw_path), script.content, out, scorer=scorer)
        except Exception as exc:  # noqa: BLE001
            _fail(session, ep, None, f"audio processing failed: {exc}")
            raise StepFailed(str(exc)) from exc
        clean = media.put_file(session, user_id=ep.series.user_id, local_path=out, kind=MediaKind.CLEAN_AUDIO,
                               episode_id=ep.id, duration_sec=result["clean_duration"],
                               meta={"raw_asset": raw.id, "method": result["method"]})
    _save_doc(session, ep, DocKind.ALIGNMENT, json.dumps(result, ensure_ascii=False, indent=1))
    ep.state = EpisodeState.AUDIO_REVIEW
    missing = result.get("missing_lines") or []
    schedule.open_task(session, user_id=ep.series.user_id, task_type=TaskType.CHECK_AUDIO, episode=ep,
                       payload={"summary": f"{result['raw_duration']}s → {result['clean_duration']}s"
                                + (f", {len(missing)} line(s) not found" if missing else ""),
                                "asset_id": clean.id, "missing_lines": missing, "method": result["method"]})


def _stage_edit(session: Session, ep: Episode, runner: Runner) -> None:
    ep.state = EpisodeState.EDITING
    clean = media.latest(session, ep.id, MediaKind.CLEAN_AUDIO)
    align = _doc(session, ep, DocKind.ALIGNMENT)
    timings = json.loads(align.content).get("lines", []) if align else []
    timing_text = "\n".join(f"{t['start']:.2f}–{t['end']:.2f}s  {t['text']}" for t in timings)
    step = f"scenes-a{clean.id}"
    run = run_step(session, ep, "visuals", step,
                   lambda: _request(resolve(session, ep, "visuals", extra=[("timings", timing_text)]), P_VISUALS,
                                    output_file="scenes.json", keep_files=("scenes/*.svg",)),
                   runner)
    files: dict[str, bytes] = getattr(run, "_files", None) or {}
    try:
        plan = extract_json(run.output_text or "")
        if not isinstance(plan, list) or not plan:
            raise ValueError("scenes.json must be a non-empty list")
        if not files:
            # Rerun after a restart: the SVGs live in the scene-plan document.
            stored = _doc(session, ep, DocKind.SCENE_PLAN)
            files = {k: v.encode() for k, v in json.loads(stored.content).get("svgs", {}).items()} if stored else {}
        scenes = []
        for item in plan:
            name = str(item.get("file", "")).lstrip("./")
            if name not in files:
                raise ValueError(f"scene file {name!r} was not produced")
            scenes.append(Scene(float(item.get("start", 0)), files[name], str(item.get("description", ""))))
    except (ValueError, KeyError, TypeError) as exc:
        run.status = RunStatus.FAILED
        run.error = f"bad scene plan: {exc}"
        _fail(session, ep, run, run.error)
        raise StepFailed(run.error) from exc
    _save_doc(session, ep, DocKind.SCENE_PLAN, json.dumps(
        {"scenes": plan, "svgs": {k: v.decode("utf-8", "replace") for k, v in files.items()}}, ensure_ascii=False))

    bgm_assets = media.library(session, ep.series.user_id, MediaKind.BGM)
    series_bgm = next((a for a in bgm_assets if (a.meta or {}).get("series") == ep.series.slug), None)
    bgm = series_bgm or (bgm_assets[0] if bgm_assets else None)
    import tempfile
    from contextlib import ExitStack

    with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
        voice = stack.enter_context(media.local_copy(clean))
        bgm_path = stack.enter_context(media.local_copy(bgm)) if bgm else None
        out = Path(tmp) / "reel.mp4"
        try:
            duration = render_reel(scenes, Path(voice), out, bgm=Path(bgm_path) if bgm_path else None)
        except Exception as exc:  # noqa: BLE001
            _fail(session, ep, run, f"render failed: {exc}")
            raise StepFailed(str(exc)) from exc
        video = media.put_file(session, user_id=ep.series.user_id, local_path=out, kind=MediaKind.VIDEO,
                               episode_id=ep.id, duration_sec=duration,
                               meta={"scenes": len(scenes), "bgm_asset": bgm.id if bgm else None})
    ep.state = EpisodeState.VIDEO_REVIEW
    schedule.open_task(session, user_id=ep.series.user_id, task_type=TaskType.APPROVE_VIDEO, episode=ep,
                       payload={"summary": f"{duration:.0f}s, {len(scenes)} scenes", "asset_id": video.id})


def _template_values(ep: Episode) -> dict[str, Any]:
    return {"series": ep.series.slug, "number": ep.number, "keyword": ep.keyword, "title": ep.title,
            "slug": integrations.slugify(ep.title)}


def _stage_automations(session: Session, ep: Episode, runner: Runner) -> None:
    ep.state = EpisodeState.AUTOMATIONS
    links: dict[str, str] = dict((ep.meta or {}).get("links", {}))
    for auto in sorted(ep.series.automations or [], key=lambda a: a.get("order", 1)):
        if not auto.get("enabled", True):
            continue
        name = auto["name"]
        run = run_step(session, ep, "automation", name,
                       lambda: _request(resolve(session, ep, "automation", automation=name), P_AUTOMATION), runner)
        _save_doc(session, ep, DocKind.AUTOMATION_OUTPUT, run.output_text or "", key=name)
        cfg = auto.get("config") or {}
        handler = cfg.get("handler", "none")
        done = (ep.meta or {}).get("automation_results", {})
        if handler == "git" and name not in done:
            try:
                res = integrations.git_publish(run.output_text or "", cfg, _template_values(ep))
            except Exception as exc:  # noqa: BLE001
                _fail(session, ep, run, f"automation {name}: {exc}")
                raise StepFailed(str(exc)) from exc
            ep.meta = {**(ep.meta or {}), "automation_results": {**done, name: res}}
            if res.get("url"):
                links[name] = res["url"]
        elif cfg.get("url"):
            links[name] = integrations._fmt(cfg["url"], _template_values(ep))
    ep.meta = {**(ep.meta or {}), "links": links}

    links_text = "\n".join(f"- {k}: {v}" for k, v in links.items())
    run = run_step(session, ep, "automation", "post-copy",
                   lambda: _request(resolve(session, ep, "post_copy", extra=[("links", links_text)]), P_POST_COPY,
                                    output_file="output.json"), runner)
    try:
        copy = extract_json(run.output_text or "")
        if not isinstance(copy, dict) or not copy.get("caption"):
            raise ValueError("missing caption")
    except ValueError as exc:
        run.status = RunStatus.FAILED
        run.error = f"bad post copy: {exc}"
        _fail(session, ep, run, run.error)
        raise StepFailed(run.error) from exc
    if ep.keyword.lower() not in copy["caption"].lower():
        copy["caption"] = copy["caption"].rstrip() + f"\n\nComment {ep.keyword} and I'll DM it to you."
    _save_doc(session, ep, DocKind.AUTOMATION_OUTPUT, json.dumps(copy, ensure_ascii=False, indent=1), key="post-copy")

    prompt = P_CONTINUITY.replace("{number}", str(ep.number)).replace("{title}", ep.title)
    run = run_step(session, ep, "automation", "continuity",
                   lambda: _request(resolve(session, ep, "continuity"), prompt), runner)
    _append_continuity(session, ep, run.output_text or "")
    ep.state = EpisodeState.PUBLISHING


def _append_continuity(session: Session, ep: Episode, entry: str) -> None:
    import re as _re

    entry = entry.strip()
    if not entry.startswith("## "):
        entry = f"## Ep {ep.number}: {ep.title}\n{entry}"
    cur = documents.read(session, ScopeType.SERIES, ep.series_id, DocKind.CONTINUITY)
    text = cur.content if cur else ""
    # replace an existing entry for this episode (rerun), else append
    pattern = _re.compile(rf"^## Ep(?:isode)?\s*{ep.number}\b.*?(?=^## |\Z)", _re.MULTILINE | _re.DOTALL)
    text = pattern.sub("", text).strip()
    text = (text + "\n\n" + entry).strip()
    documents.write(session, user_id=ep.series.user_id, scope_type=ScopeType.SERIES, scope_id=ep.series_id,
                    kind=DocKind.CONTINUITY, content=text, source=VersionSource.PIPELINE, note=f"Ep {ep.number}")


def _stage_publish(session: Session, ep: Episode, runner: Runner) -> None:
    if ep.slot_kind == SlotKind.AFTER and ep.after_episode:
        prior = next((e for e in ep.series.episodes if e.number == ep.after_episode), None)
        if prior is not None and prior.state != EpisodeState.LIVE:
            return  # waits for the earlier episode
    copy_ref = _doc(session, ep, DocKind.AUTOMATION_OUTPUT, "post-copy")
    copy = json.loads(copy_ref.content) if copy_ref else {"caption": ep.title, "hashtags": [], "dm_message": ""}
    caption = copy["caption"].rstrip()
    if copy.get("hashtags"):
        caption += "\n\n" + " ".join(h if h.startswith("#") else f"#{h}" for h in copy["hashtags"])
    video = media.latest(session, ep.id, MediaKind.VIDEO)
    server = integrations.superprofile_server(session, ep.series.user_id)
    url = media.public_url(video) if video else None
    at = schedule.publish_at(ep)
    if not server or not url:
        reason = "SuperProfile is not set up" if not server else "the video has no public URL (set REELD_PUBLIC_BASE_URL or STORAGE=s3)"
        schedule.open_task(session, user_id=ep.series.user_id, task_type=TaskType.PUBLISH, episode=ep,
                           payload={"summary": f"Post it yourself: {reason}", "caption": caption,
                                    "dm_message": copy.get("dm_message", ""), "keyword": ep.keyword,
                                    "asset_id": video.id if video else None,
                                    "scheduled_for": at.isoformat() if at else None})
        return
    when = f"at {at.isoformat()}" if at and at > now() else "published now"
    prompt = P_PUBLISH.format(server=f"mcp__{server}__*", video_url=url, when=when, keyword=ep.keyword,
                              caption=caption, dm=copy.get("dm_message", ""))
    ctx = resolve(session, ep, "publish")
    run = run_step(session, ep, "publish", "superprofile",
                   lambda: _request(ctx, prompt, output_file="output.json",
                                    allowed_tools=["Read", "Write", f"mcp__{server}"]), runner)
    try:
        res = extract_json(run.output_text or "")
    except ValueError:
        res = {"scheduled": False, "notes": (run.output_text or "")[:300]}
    if not res.get("scheduled"):
        run.status = RunStatus.FAILED
        run.error = f"SuperProfile did not schedule the reel: {res.get('notes', '')}"
        _fail(session, ep, run, run.error)
        raise StepFailed(run.error)
    mark_published(session, ep, res)


def mark_published(session: Session, ep: Episode, details: dict[str, Any]) -> None:
    ep.meta = {**(ep.meta or {}), "published": {**details, "at": now().isoformat()}}
    ep.state = EpisodeState.LIVE
    schedule.close_tasks(session, ep, TaskType.PUBLISH, TaskType.FIX_FAILURE)


# ─── Driver ───────────────────────────────────────────────────────────────────

ACTIVE_STATES = {EpisodeState.SCRIPTING, EpisodeState.AUDIO_PROCESSING, EpisodeState.EDITING,
                 EpisodeState.AUTOMATIONS, EpisodeState.PUBLISHING}


@dataclass
class AdvanceResult:
    episode_id: int
    state: str
    paused_until: Optional[str] = None
    error: Optional[str] = None


def advance(session: Session, episode: Episode, runner: Optional[Runner] = None, scorer=None) -> AdvanceResult:
    """Move one episode as far as it can go. Commits as it goes."""
    runner = runner or get_runner()
    try:
        for _ in range(12):  # each pass moves at most one stage
            before = episode.state
            if episode.state in (EpisodeState.PLANNED, EpisodeState.SCRIPTING):
                _stage_script(session, episode, runner)
            elif episode.state == EpisodeState.AUDIO_PROCESSING:
                _stage_audio(session, episode, scorer=scorer)
            elif episode.state == EpisodeState.EDITING:
                _stage_edit(session, episode, runner)
            elif episode.state == EpisodeState.AUTOMATIONS:
                _stage_automations(session, episode, runner)
            elif episode.state == EpisodeState.PUBLISHING:
                _stage_publish(session, episode, runner)
            session.commit()
            if episode.state == before or episode.state not in ACTIVE_STATES:
                break
        episode.meta = {k: v for k, v in (episode.meta or {}).items() if k not in ("error", "failed_step")}
        session.commit()
        return AdvanceResult(episode.id, episode.state.value)
    except StepPaused as exc:
        session.commit()
        return AdvanceResult(episode.id, episode.state.value, paused_until=exc.resume_at.isoformat())
    except StepFailed as exc:
        session.commit()
        return AdvanceResult(episode.id, episode.state.value, error=str(exc))


def episodes_to_advance(session: Session, user_id: Optional[int] = None) -> list[Episode]:
    """Episodes the worker should push forward now.

    Active-state episodes always; planned ones only for active series, up to
    REELD_SCRIPTS_AHEAD episodes in flight per series, earliest slot first.
    """
    q = select(Series).where(Series.status == SeriesStatus.ACTIVE)
    if user_id is not None:
        q = q.where(Series.user_id == user_id)
    picked: list[Episode] = []
    for series in session.scalars(q):
        eps = list(series.episodes)
        picked += [e for e in eps if e.state in ACTIVE_STATES and not (e.meta or {}).get("error")]
        in_flight = sum(1 for e in eps if e.state not in (EpisodeState.PLANNED, EpisodeState.LIVE))
        room = max(0, SCRIPTS_AHEAD - in_flight)
        planned = sorted((e for e in eps if e.state == EpisodeState.PLANNED),
                         key=lambda e: (schedule.publish_at(e) is None, schedule.publish_at(e) or now(), e.number))
        picked += planned[:room]
    return picked


# ─── Gate actions (called from the API) ───────────────────────────────────────


def approve_script(session: Session, ep: Episode, content: Optional[str] = None) -> None:
    if ep.state != EpisodeState.SCRIPT_REVIEW:
        raise ValueError(f"episode is {ep.state.value}, not waiting for script approval")
    if content is not None:
        ref = _doc(session, ep, DocKind.SCRIPT)
        if ref is None or content.strip() != ref.content.strip():
            _save_doc(session, ep, DocKind.SCRIPT, content, source=VersionSource.USER, note="edited at approval")
    ep.meta = {**(ep.meta or {}), "script_approved_at": now().isoformat()}
    ep.state = EpisodeState.AWAITING_RECORDING
    schedule.close_tasks(session, ep, TaskType.APPROVE_SCRIPT)
    schedule.open_task(session, user_id=ep.series.user_id, task_type=TaskType.RECORD, episode=ep,
                       payload={"summary": "Read each line; pause and repeat a line if you slip"})


def script_feedback(session: Session, ep: Episode, note: str, runner: Optional[Runner] = None) -> AdvanceResult:
    """Ask Claude to revise the script with the creator's note (stays at gate 1)."""
    if ep.state != EpisodeState.SCRIPT_REVIEW:
        raise ValueError("feedback is only possible while the script waits for approval")
    runner = runner or get_runner()
    current = _doc(session, ep, DocKind.SCRIPT)
    n = sum(1 for r in session.scalars(select(StageRun).where(
        StageRun.episode_id == ep.id, StageRun.stage == "script")) if r.step.startswith("revise-u")) + 1
    try:
        run = run_step(session, ep, "script", f"revise-u{n}",
                       lambda: _request(resolve(session, ep, "revise", draft=current.content if current else "",
                                                extra=[("feedback", note)]), P_REVISE_USER), runner)
    except StepPaused as exc:
        session.commit()
        return AdvanceResult(ep.id, ep.state.value, paused_until=exc.resume_at.isoformat())
    except StepFailed as exc:
        session.commit()
        return AdvanceResult(ep.id, ep.state.value, error=str(exc))
    _save_doc(session, ep, DocKind.SCRIPT, run.output_text or "", note=f"revised from feedback: {note[:120]}")
    session.commit()
    return AdvanceResult(ep.id, ep.state.value)


def attach_recording(session: Session, ep: Episode, local_file: Path) -> None:
    if ep.state not in (EpisodeState.AWAITING_RECORDING, EpisodeState.AUDIO_REVIEW):
        raise ValueError(f"episode is {ep.state.value}, not waiting for a recording")
    media.put_file(session, user_id=ep.series.user_id, local_path=local_file, kind=MediaKind.RAW_TAKE,
                   episode_id=ep.id)
    schedule.close_tasks(session, ep, TaskType.RECORD, TaskType.CHECK_AUDIO)
    ep.state = EpisodeState.AUDIO_PROCESSING


def approve_audio(session: Session, ep: Episode, cuts: Optional[list[tuple[float, float]]] = None) -> None:
    if ep.state != EpisodeState.AUDIO_REVIEW:
        raise ValueError(f"episode is {ep.state.value}, not waiting for audio approval")
    if cuts:
        clean = media.latest(session, ep.id, MediaKind.CLEAN_AUDIO)
        import tempfile

        with media.local_copy(clean) as src, tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clean-cut.m4a"
            dur = audio_mod.apply_extra_cuts(Path(src), cuts, out)
            media.put_file(session, user_id=ep.series.user_id, local_path=out, kind=MediaKind.CLEAN_AUDIO,
                           episode_id=ep.id, duration_sec=dur, meta={"cut_from": clean.id, "cuts": cuts})
        align = _doc(session, ep, DocKind.ALIGNMENT)
        if align:
            data = json.loads(align.content)
            data["lines"] = _shift_timings(data.get("lines", []), cuts)
            data["extra_cuts"] = cuts
            _save_doc(session, ep, DocKind.ALIGNMENT, json.dumps(data, ensure_ascii=False, indent=1),
                      source=VersionSource.USER, note="extra cuts")
    schedule.close_tasks(session, ep, TaskType.CHECK_AUDIO)
    ep.state = EpisodeState.EDITING


def _shift_timings(lines: list[dict], cuts: list[tuple[float, float]]) -> list[dict]:
    def shift(t: float) -> float:
        removed = 0.0
        for s, e in sorted(cuts):
            if t >= e:
                removed += e - s
            elif t > s:
                removed += t - s
        return round(max(0.0, t - removed), 3)

    return [{**l, "start": shift(l["start"]), "end": shift(l["end"])} for l in lines]


def approve_video(session: Session, ep: Episode) -> None:
    if ep.state != EpisodeState.VIDEO_REVIEW:
        raise ValueError(f"episode is {ep.state.value}, not waiting for video approval")
    schedule.close_tasks(session, ep, TaskType.APPROVE_VIDEO)
    ep.state = EpisodeState.AUTOMATIONS


def redo_video(session: Session, ep: Episode, note: str = "") -> None:
    """Throw away the scene plan and render again (optionally guided by a note)."""
    if ep.state != EpisodeState.VIDEO_REVIEW:
        raise ValueError("only possible while the video waits for approval")
    clean = media.latest(session, ep.id, MediaKind.CLEAN_AUDIO)
    for r in session.scalars(select(StageRun).where(StageRun.episode_id == ep.id, StageRun.stage == "visuals",
                                                    StageRun.step == f"scenes-a{clean.id}")):
        r.step = f"{r.step}-discarded-{r.id}"
    if note:
        ep.overrides = {**(ep.overrides or {}), "Visual notes for this episode": note}
    schedule.close_tasks(session, ep, TaskType.APPROVE_VIDEO, status=TaskStatus.DISMISSED)
    ep.state = EpisodeState.EDITING


def retry(session: Session, ep: Episode) -> None:
    """Clear a failure so the worker tries the failed step again."""
    ep.meta = {k: v for k, v in (ep.meta or {}).items() if k not in ("error", "failed_step")}
    schedule.close_tasks(session, ep, TaskType.FIX_FAILURE)


def resumable_runs(session: Session) -> list[StageRun]:
    return [r for r in session.scalars(select(StageRun).where(StageRun.status == RunStatus.PAUSED))
            if r.resume_at is None or _utc(r.resume_at) <= now()]
