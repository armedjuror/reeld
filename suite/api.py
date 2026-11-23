"""HTTP API for the task-first UI (spec §7). Mounted at /api/suite.

Run alone:          uvicorn suite.app:app --reload
Or inside main.py:  app.include_router(suite.api.router)  (already wired)
"""

from __future__ import annotations

import difflib
import json
import shutil
import tempfile
from datetime import date, timedelta
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import documents, insights, integrations, media, pipeline, schedule
from .db import session_scope
from .models import (
    DocKind,
    DocumentVersion,
    Episode,
    EpisodeState,
    MediaAsset,
    MediaKind,
    Proposal,
    ScopeType,
    Series,
    SeriesStatus,
    SlotKind,
    StageRun,
    Task,
    TaskStatus,
    TaskType,
    VersionSource,
)
from .users import UserNotConfigured, current_user_id

router = APIRouter(prefix="/api/suite", tags=["suite"])


def get_session():
    with session_scope() as s:
        yield s


def get_user(request: Request, s: Session = Depends(get_session)) -> int:
    """The signed-in user (legacy session) or the configured local user."""
    try:
        uid = request.session.get("user_id") if "session" in request.scope else None
    except AssertionError:
        uid = None
    if uid:
        return int(uid)
    try:
        return current_user_id(s)
    except UserNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc


def _kick(episode_id: int) -> None:
    """Advance in the background; failures to reach the broker don't break the request."""
    try:
        from .worker import kick

        if not kick(episode_id):
            print(f"[suite] worker unreachable; episode {episode_id} will move on the next tick")
    except Exception as exc:  # noqa: BLE001
        print(f"[suite] could not queue episode {episode_id}: {exc}")


def _episode(s: Session, uid: int, episode_id: int) -> Episode:
    ep = s.get(Episode, episode_id)
    if ep is None or ep.series.user_id != uid:
        raise HTTPException(404, "episode not found")
    return ep


def _text(s: Session, ep: Episode, kind: DocKind, key: str = "main") -> Optional[str]:
    ref = documents.read(s, ScopeType.EPISODE, ep.id, kind, key)
    return ref.content if ref else None


def _media_url(asset: Optional[MediaAsset]) -> Optional[str]:
    return f"/api/suite/media/{asset.id}" if asset else None


# ─── Home ─────────────────────────────────────────────────────────────────────


@router.get("/home")
def home(uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict[str, Any]:
    today = schedule.local_today()
    paused = pipeline.claude_paused_until(s)
    digest = documents.read(s, ScopeType.USER, uid, DocKind.DIGEST)
    tasks = schedule.task_list(s, uid)
    working = [
        {"episode_id": e.id, "series": e.series.name, "number": e.number, "title": e.title, "state": e.state.value}
        for e in s.scalars(select(Episode).join(Series).where(
            Series.user_id == uid, Episode.state.in_(list(pipeline.ACTIVE_STATES))))
    ]
    return {
        "today": schedule.day_strip(s, uid, today),
        "tomorrow": schedule.day_strip(s, uid, today + timedelta(days=1)),
        "date": today.isoformat(),
        "tasks": [t.__dict__ for t in tasks],
        "paused_until": paused.isoformat() if paused else None,
        "working": working,
        "digest": digest.content if digest else None,
        "series_count": len(list(s.scalars(select(Series.id).where(Series.user_id == uid)))),
    }


@router.post("/open")
def app_opened(uid: int = Depends(get_user)) -> dict:
    """Called when the app opens: kick off the daily stats pull and analysis."""
    try:
        from .worker import broker_ok, daily

        if not broker_ok():
            return {"queued": False, "error": "worker not running (start Redis and the Celery worker)"}
        daily.delay(uid)
        return {"queued": True}
    except Exception as exc:  # noqa: BLE001
        return {"queued": False, "error": str(exc)}


@router.post("/tasks/{task_id}/dismiss")
def dismiss_task(task_id: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    t = s.get(Task, task_id)
    if t is None or t.user_id != uid:
        raise HTTPException(404, "task not found")
    if t.type not in (TaskType.READ_INSIGHTS, TaskType.FIX_FAILURE):
        raise HTTPException(400, "finish this task instead of dismissing it")
    t.status = TaskStatus.DONE if t.type == TaskType.READ_INSIGHTS else TaskStatus.DISMISSED
    return {"ok": True}


# ─── Episodes ─────────────────────────────────────────────────────────────────


@router.get("/episodes/{episode_id}")
def episode_detail(episode_id: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    critic = _text(s, ep, DocKind.CRITIC)
    align = _text(s, ep, DocKind.ALIGNMENT)
    copy = _text(s, ep, DocKind.AUTOMATION_OUTPUT, "post-copy")
    autos = {d.key: documents.current(s, d).content for d in documents.list_docs(s, ScopeType.EPISODE, ep.id,
                                                                                 DocKind.AUTOMATION_OUTPUT)
             if d.key != "post-copy" and documents.current(s, d)}
    runs = list(s.scalars(select(StageRun).where(StageRun.episode_id == ep.id).order_by(StageRun.id)))
    script_doc = documents.find(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT)
    versions = []
    if script_doc:
        versions = [{"version": v.version, "source": v.source.value, "note": v.note}
                    for v in s.scalars(select(DocumentVersion).where(DocumentVersion.document_id == script_doc.id)
                                       .order_by(DocumentVersion.version))]
    return {
        "id": ep.id, "series": ep.series.name, "series_slug": ep.series.slug, "number": ep.number,
        "title": ep.title, "hook": ep.hook, "core_idea": ep.core_idea, "keyword": ep.keyword,
        "state": ep.state.value, "meta": ep.meta or {},
        "slot": {"kind": ep.slot_kind.value, "date": ep.slot_date.isoformat() if ep.slot_date else None,
                 "time": ep.slot_time.strftime("%H:%M") if ep.slot_time else None, "after": ep.after_episode},
        "research": _text(s, ep, DocKind.RESEARCH),
        "script": _text(s, ep, DocKind.SCRIPT),
        "script_versions": versions,
        "critic": json.loads(critic) if critic else None,
        "alignment": json.loads(align) if align else None,
        "audio_url": _media_url(media.latest(s, ep.id, MediaKind.CLEAN_AUDIO)),
        "raw_url": _media_url(media.latest(s, ep.id, MediaKind.RAW_TAKE)),
        "video_url": _media_url(media.latest(s, ep.id, MediaKind.VIDEO)),
        "post_copy": json.loads(copy) if copy else None,
        "automations": autos,
        "metrics": insights.latest_metrics(s, ep.id),
        "runs": [{"stage": r.stage, "step": r.step, "status": r.status.value, "attempt": r.attempt,
                  "error": r.error, "resume_at": r.resume_at.isoformat() if r.resume_at else None,
                  "cost_usd": r.cost_usd, "tokens": (r.context_manifest or {}).get("tokens_estimate")}
                 for r in runs],
    }


class ScriptApproval(BaseModel):
    content: Optional[str] = None


@router.post("/episodes/{episode_id}/approve-script")
def approve_script(episode_id: int, body: ScriptApproval, uid: int = Depends(get_user),
                   s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        pipeline.approve_script(s, ep, body.content)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"state": ep.state.value}


class Feedback(BaseModel):
    note: str


@router.post("/episodes/{episode_id}/script-feedback")
def script_feedback(episode_id: int, body: Feedback, uid: int = Depends(get_user),
                    s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        res = pipeline.script_feedback(s, ep, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return res.__dict__


@router.post("/episodes/{episode_id}/recording")
def upload_recording(episode_id: int, file: UploadFile = File(...), uid: int = Depends(get_user),
                     s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    suffix = Path(file.filename or "take.webm").suffix or ".webm"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"take{suffix}"
        with path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        try:
            pipeline.attach_recording(s, ep, path)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    s.commit()
    _kick(ep.id)
    return {"state": ep.state.value}


class AudioApproval(BaseModel):
    cuts: list[tuple[float, float]] = []


@router.post("/episodes/{episode_id}/approve-audio")
def approve_audio(episode_id: int, body: AudioApproval, uid: int = Depends(get_user),
                  s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        pipeline.approve_audio(s, ep, [tuple(c) for c in body.cuts] or None)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    s.commit()
    _kick(ep.id)
    return {"state": ep.state.value}


@router.post("/episodes/{episode_id}/approve-video")
def approve_video(episode_id: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        pipeline.approve_video(s, ep)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    s.commit()
    _kick(ep.id)
    return {"state": ep.state.value}


class RedoVideo(BaseModel):
    note: str = ""


@router.post("/episodes/{episode_id}/redo-video")
def redo_video(episode_id: int, body: RedoVideo, uid: int = Depends(get_user),
               s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        pipeline.redo_video(s, ep, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    s.commit()
    _kick(ep.id)
    return {"state": ep.state.value}


class Published(BaseModel):
    permalink: Optional[str] = None


@router.post("/episodes/{episode_id}/mark-published")
def mark_published(episode_id: int, body: Published, uid: int = Depends(get_user),
                   s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    if ep.state != EpisodeState.PUBLISHING:
        raise HTTPException(409, f"episode is {ep.state.value}")
    pipeline.mark_published(s, ep, {"manual": True, "permalink": body.permalink})
    return {"state": ep.state.value}


@router.post("/episodes/{episode_id}/retry")
def retry(episode_id: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    pipeline.retry(s, ep)
    s.commit()
    _kick(ep.id)
    return {"state": ep.state.value}


class SlotIn(BaseModel):
    kind: str
    date: Optional[_date] = None
    time: Optional[str] = None
    after_episode: Optional[int] = None


@router.post("/episodes/{episode_id}/slot")
def set_slot(episode_id: int, body: SlotIn, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ep = _episode(s, uid, episode_id)
    try:
        kind = SlotKind(body.kind)
    except ValueError as exc:
        raise HTTPException(400, "bad slot kind") from exc
    if kind in (SlotKind.PRECISE, SlotKind.WEEK, SlotKind.MONTH) and body.date is None:
        raise HTTPException(400, "this slot needs a date")
    from datetime import time as dtime

    ep.slot_kind = kind
    ep.slot_date = body.date
    ep.slot_time = dtime.fromisoformat(body.time) if (body.time and kind == SlotKind.PRECISE) else None
    ep.after_episode = body.after_episode if kind == SlotKind.AFTER else None
    schedule.refresh_due_dates(s, ep)
    return {"ok": True}


# ─── Calendar and series ──────────────────────────────────────────────────────


@router.get("/calendar")
def calendar(start: Optional[date] = None, days: int = 42, uid: int = Depends(get_user),
             s: Session = Depends(get_session)) -> dict:
    start = start or schedule.local_today().replace(day=1)
    end = start + timedelta(days=days)
    return {"start": start.isoformat(), "end": end.isoformat(), "items": schedule.calendar(s, uid, start, end)}


@router.get("/series")
def list_series(uid: int = Depends(get_user), s: Session = Depends(get_session)) -> list[dict]:
    out = []
    for sr in s.scalars(select(Series).where(Series.user_id == uid).order_by(Series.name)):
        out.append({"slug": sr.slug, "name": sr.name, "status": sr.status.value,
                    "episodes": [{"id": e.id, "number": e.number, "title": e.title, "state": e.state.value,
                                  "slot_kind": e.slot_kind.value,
                                  "slot_date": e.slot_date.isoformat() if e.slot_date else None,
                                  "error": (e.meta or {}).get("error")} for e in sr.episodes]})
    return out


class SeriesStatusIn(BaseModel):
    status: str


@router.post("/series/{slug}/status")
def set_series_status(slug: str, body: SeriesStatusIn, uid: int = Depends(get_user),
                      s: Session = Depends(get_session)) -> dict:
    sr = s.scalar(select(Series).where(Series.user_id == uid, Series.slug == slug))
    if sr is None:
        raise HTTPException(404, "series not found")
    try:
        sr.status = SeriesStatus(body.status)
    except ValueError as exc:
        raise HTTPException(400, "bad status") from exc
    s.commit()
    if sr.status == SeriesStatus.ACTIVE:
        for e in pipeline.episodes_to_advance(s, uid):
            if e.series_id == sr.id:
                _kick(e.id)
    return {"status": sr.status.value}


# ─── Proposals ────────────────────────────────────────────────────────────────


def _proposal(s: Session, uid: int, pid: int) -> Proposal:
    p = s.get(Proposal, pid)
    if p is None or p.user_id != uid:
        raise HTTPException(404, "proposal not found")
    return p


@router.get("/proposals/{pid}")
def proposal_detail(pid: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    p = _proposal(s, uid, pid)
    doc = s.get(documents.Document, p.document_id)
    cur = documents.current(s, doc)
    old = cur.content if cur else ""
    diff = list(difflib.unified_diff(old.splitlines(), p.proposed_content.splitlines(), "current", "proposed",
                                     lineterm="", n=2))
    series = s.get(Series, p.series_id) if p.series_id else None
    label = {"learnings": "learnings", "bible": "series bible"}.get(doc.kind.value, f"{doc.key} skill")
    return {"id": p.id, "series": series.name if series else None, "target": f"{doc.kind.value}:{doc.key}",
            "label": label,
            "rationale": p.rationale, "source": p.source.value, "status": p.status.value,
            "current": old, "proposed": p.proposed_content, "diff": diff,
            "stale": doc.current_version != p.base_version}


class ProposalDecision(BaseModel):
    content: Optional[str] = None


@router.post("/proposals/{pid}/accept")
def accept(pid: int, body: ProposalDecision, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    try:
        return insights.accept_proposal(s, _proposal(s, uid, pid), body.content)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/proposals/{pid}/reject")
def reject(pid: int, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    insights.reject_proposal(s, _proposal(s, uid, pid))
    return {"ok": True}


# ─── Settings ─────────────────────────────────────────────────────────────────


@router.get("/profile")
def get_profile(uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ref = documents.read(s, ScopeType.USER, uid, DocKind.VOICE)
    return {"content": ref.content if ref else "", "version": ref.version if ref else 0}


class ProfileIn(BaseModel):
    content: str


@router.put("/profile")
def put_profile(body: ProfileIn, uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    ref, changed = documents.write(s, user_id=uid, scope_type=ScopeType.USER, scope_id=uid, kind=DocKind.VOICE,
                                   content=body.content, source=VersionSource.USER)
    return {"version": ref.version, "changed": changed}


@router.get("/integrations")
def get_integrations(uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    return {p: integrations.public_view(integrations.get(s, uid, p)) for p in ("meta", "superprofile")}


class IntegrationIn(BaseModel):
    data: dict[str, Any]


@router.put("/integrations/{provider}")
def put_integration(provider: str, body: IntegrationIn, uid: int = Depends(get_user),
                    s: Session = Depends(get_session)) -> dict:
    if provider not in ("meta", "superprofile"):
        raise HTTPException(404, "unknown integration")
    data = {k: v for k, v in body.data.items() if v not in (None, "") and "…" not in str(v)}
    if provider == "meta" and data.get("access_token"):
        from datetime import datetime, timezone

        data.setdefault("expires_at", (datetime.now(timezone.utc) + timedelta(days=60)).isoformat())
    return integrations.public_view(integrations.put(s, uid, provider, data))


@router.get("/library")
def get_library(uid: int = Depends(get_user), s: Session = Depends(get_session)) -> list[dict]:
    return [{"id": a.id, "kind": a.kind.value, "name": (a.meta or {}).get("name"), "series": (a.meta or {}).get("series"),
             "url": _media_url(a)} for k in (MediaKind.BGM, MediaKind.SFX) for a in media.library(s, uid, k)]


@router.post("/library")
def add_library(kind: str = "bgm", series: Optional[str] = None, file: UploadFile = File(...),
                uid: int = Depends(get_user), s: Session = Depends(get_session)) -> dict:
    mk = MediaKind(kind)
    if mk not in (MediaKind.BGM, MediaKind.SFX):
        raise HTTPException(400, "library takes bgm or sfx")
    data = file.file.read()
    a = media.put_bytes(s, user_id=uid, data=data, filename=file.filename or "track.mp3", kind=mk,
                        meta={"name": file.filename, "series": series})
    return {"id": a.id}


# ─── Media ────────────────────────────────────────────────────────────────────


@router.get("/media/{asset_id}")
def serve_media(asset_id: int, s: Session = Depends(get_session)):
    """Unauthenticated by id so SuperProfile can fetch the video via a public base URL."""
    a = s.get(MediaAsset, asset_id)
    if a is None:
        raise HTTPException(404, "not found")
    local = media.local_path(a)
    if local is None:
        return RedirectResponse(media.public_url(a, expires=3600))
    if not local.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(local, media_type=a.mime or "application/octet-stream")
