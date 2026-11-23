"""Insights, analysis and self-improvement (spec §6).

- `MetaClient`: Instagram API with Instagram Login (graph.instagram.com).
  Long-lived token stored in the `meta` integration, refreshed when it has
  under a week left.
- `pull_insights`: links live episodes to their reels (by comment keyword in
  the caption) and stores an `InsightSnapshot` per reel per pull.
- `run_analysis`: one Claude step per active series with enough live reels;
  writes a digest and proposals (never applies them).
- `maybe_distil`: every N approved scripts, turns the creator's edits into a
  proposed learnings update.
- `accept_proposal` / `reject_proposal`: the one-click approval.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import documents, integrations, schedule
from .models import (
    DocKind,
    DocumentVersion,
    Episode,
    EpisodeState,
    InsightSnapshot,
    Proposal,
    ProposalStatus,
    RunStatus,
    ScopeType,
    Series,
    SeriesStatus,
    StageRun,
    TaskType,
    VersionSource,
)
from .pipeline import StepFailed, StepPaused, run_step
from .runner import Runner, StepRequest, extract_json, get_runner

GRAPH = os.environ.get("REELD_META_GRAPH", "https://graph.instagram.com")
API_VERSION = os.environ.get("REELD_META_API_VERSION", "v27.0")
REEL_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
                "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate"]
MIN_LIVE_FOR_ANALYSIS = int(os.environ.get("REELD_MIN_LIVE_FOR_ANALYSIS", "3"))
LEARNINGS_CAP = 40
PULL_INTERVAL = timedelta(hours=20)
DISTIL_EVERY = int(os.environ.get("REELD_DISTIL_EVERY", "10"))


class MetaError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, token: str, transport: Optional[httpx.BaseTransport] = None):
        self.token = token
        self.http = httpx.Client(base_url=GRAPH, timeout=30, transport=transport)

    def _get(self, path: str, **params) -> dict[str, Any]:
        r = self.http.get(path, params={**params, "access_token": self.token})
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise MetaError(f"{r.status_code}: {msg[:300]}")
        return r.json()

    def refresh(self) -> dict[str, Any]:
        """Extend a long-lived token by ~60 days."""
        return self._get("/refresh_access_token", grant_type="ig_refresh_token")

    def me(self) -> dict[str, Any]:
        return self._get(f"/{API_VERSION}/me", fields="user_id,username")

    def list_media(self, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        data = self._get(f"/{API_VERSION}/me/media",
                         fields="id,caption,media_type,media_product_type,timestamp,permalink", limit=25)
        while True:
            out += data.get("data", [])
            nxt = (data.get("paging") or {}).get("next")
            if not nxt or len(out) >= limit:
                return out[:limit]
            r = self.http.get(nxt)
            if r.status_code >= 400:
                return out[:limit]
            data = r.json()

    def media_insights(self, media_id: str, metrics: list[str] = REEL_METRICS) -> dict[str, Any]:
        """Metric name → value. Metrics the API rejects are skipped individually."""
        try:
            data = self._get(f"/{API_VERSION}/{media_id}/insights", metric=",".join(metrics))
            return _flatten(data)
        except MetaError:
            out: dict[str, Any] = {}
            for m in metrics:
                try:
                    out.update(_flatten(self._get(f"/{API_VERSION}/{media_id}/insights", metric=m)))
                except MetaError:
                    continue
            return out


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for item in data.get("data", []):
        vals = item.get("values") or []
        if vals:
            out[item["name"]] = vals[0].get("value")
        elif "total_value" in item:
            out[item["name"]] = item["total_value"].get("value")
    return out


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def client_for(session: Session, user_id: int, transport: Optional[httpx.BaseTransport] = None) -> Optional[MetaClient]:
    """A client with a fresh token, or None if Meta isn't connected."""
    cfg = integrations.get(session, user_id, "meta")
    token = cfg.get("access_token") or os.environ.get("REELD_META_TOKEN")
    if not token:
        return None
    client = MetaClient(token, transport=transport)
    expires = cfg.get("expires_at")
    if expires and datetime.fromisoformat(expires) - datetime.now(timezone.utc) < timedelta(days=7):
        try:
            res = client.refresh()
            client.token = res["access_token"]
            integrations.put(session, user_id, "meta", {
                "access_token": res["access_token"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=int(res.get("expires_in", 5184000)))).isoformat(),
            })
        except MetaError:
            pass  # keep the old token until it actually fails
    return client


def link_media(session: Session, user_id: int, media_list: list[dict[str, Any]]) -> int:
    """Attach Instagram media ids to live episodes whose keyword is in the caption."""
    eps = list(session.scalars(
        select(Episode).join(Series).where(Series.user_id == user_id, Episode.state == EpisodeState.LIVE)))
    taken = {(e.meta or {}).get("instagram_media_id") for e in eps}
    linked = 0
    for ep in eps:
        if (ep.meta or {}).get("instagram_media_id"):
            continue
        published = (ep.meta or {}).get("published", {}).get("at")
        since = datetime.fromisoformat(published) - timedelta(days=2) if published else None
        kw = ep.keyword.lower()
        for m in sorted(media_list, key=lambda m: m.get("timestamp", "")):
            if m["id"] in taken or m.get("media_product_type") not in (None, "REELS"):
                continue
            caption = (m.get("caption") or "").lower()
            ts = m.get("timestamp")
            if since and ts:
                when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
                if when < since:
                    continue
            if f" {kw}" in f" {caption}".replace("\n", " ") or f"\"{kw}\"" in caption or f"'{kw}'" in caption:
                ep.meta = {**(ep.meta or {}), "instagram_media_id": m["id"], "permalink": m.get("permalink")}
                taken.add(m["id"])
                linked += 1
                break
    session.flush()
    return linked


def pull_insights(session: Session, user_id: int, client: Optional[MetaClient] = None, force: bool = False) -> dict:
    cfg = integrations.get(session, user_id, "meta")
    last = cfg.get("last_pull")
    if not force and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < PULL_INTERVAL:
        return {"skipped": "pulled recently", "last_pull": last}
    client = client or client_for(session, user_id)
    if client is None:
        return {"skipped": "meta not connected"}
    media_list = client.list_media()
    linked = link_media(session, user_id, media_list)
    snaps = 0
    for ep in session.scalars(select(Episode).join(Series).where(Series.user_id == user_id)):
        mid = (ep.meta or {}).get("instagram_media_id")
        if not mid:
            continue
        metrics = client.media_insights(mid)
        session.add(InsightSnapshot(episode_id=ep.id, platform="instagram", external_id=mid, metrics=metrics))
        snaps += 1
    integrations.put(session, user_id, "meta", {"last_pull": datetime.now(timezone.utc).isoformat()})
    session.flush()
    return {"media": len(media_list), "linked": linked, "snapshots": snaps}


def latest_metrics(session: Session, episode_id: int) -> Optional[dict[str, Any]]:
    snap = session.scalar(select(InsightSnapshot).where(InsightSnapshot.episode_id == episode_id)
                          .order_by(InsightSnapshot.captured_at.desc(), InsightSnapshot.id.desc()).limit(1))
    return {**snap.metrics, "captured_at": _utc(snap.captured_at).isoformat()} if snap else None


# ─── Analysis and proposals ───────────────────────────────────────────────────

TARGETS = {  # proposal target → (kind, key)
    "learnings": (DocKind.LEARNINGS, "main"),
    "rubric": (DocKind.SKILL, "rubric"),
    "bible": (DocKind.BIBLE, "main"),
    "script": (DocKind.SKILL, "script"),
    "visuals": (DocKind.SKILL, "visuals"),
}

P_ANALYSIS = """\
You are the analyst for a reel series. context.md has the series profile, its
current learnings, rubric and script skill, and a table of every live episode
with its latest Instagram metrics.

1. Write a short digest for the creator (markdown, at most 8 lines): what is
   working, what isn't, with numbers, compared with this series' own baseline.
2. Propose changes only for patterns that hold across at least 3 episodes.
   One reel's result is noise. If nothing clears that bar, propose nothing.
   Each proposal replaces one document in full: target is one of
   learnings, rubric, bible, script, visuals. Learnings stay a bullet list of
   at most {cap} rules (merge rather than append when full).

Write output.json only:
{{"digest": "...", "proposals": [{{"target": "learnings", "new_content": "full text", "rationale": "evidence with numbers"}}]}}
"""

P_DISTIL = """\
context.md holds pairs of scripts: what the pipeline drafted and what the
creator approved after editing. Find the consistent patterns in the edits
(at least 2 occurrences) and fold them into the learnings as short, checkable
rules. Keep existing rules unless an edit contradicts them; at most {cap}
rules, merging when needed. Write output.json only:
{{"learnings": "full new learnings markdown (bullet list)", "rationale": "the patterns, with examples"}}
"""


def _series_doc(session: Session, series: Series, target: str):
    kind, key = TARGETS[target]
    return documents.find(session, ScopeType.SERIES, series.id, kind, key), documents.read(
        session, ScopeType.SERIES, series.id, kind, key)


def _make_proposal(session: Session, series: Series, target: str, content: str, rationale: str,
                   source: VersionSource) -> Optional[Proposal]:
    if target not in TARGETS or not content.strip():
        return None
    doc, ref = _series_doc(session, series, target)
    if doc is None:
        kind, key = TARGETS[target]
        ref, _ = documents.write(session, user_id=series.user_id, scope_type=ScopeType.SERIES, scope_id=series.id,
                                 kind=kind, key=key, content="", source=source)
        doc = documents.find(session, ScopeType.SERIES, series.id, kind, key)
    if ref and ref.content.strip() == content.strip():
        return None
    prop = Proposal(user_id=series.user_id, series_id=series.id, document_id=doc.id,
                    base_version=doc.current_version, proposed_content=content.strip() + "\n",
                    rationale=rationale, source=source)
    session.add(prop)
    session.flush()
    schedule.open_task(session, user_id=series.user_id, task_type=TaskType.REVIEW_PROPOSAL, series_id=series.id,
                       payload={"summary": (rationale or "")[:140], "proposal_id": prop.id,
                                "target": target})
    return prop


def _metrics_table(session: Session, eps: list[Episode]) -> str:
    cols = ["views", "reach", "ig_reels_avg_watch_time", "likes", "comments", "shares", "saved"]
    rows = ["| Ep | Title | Hook | " + " | ".join(cols) + " |", "|" + "---|" * (3 + len(cols))]
    for ep in eps:
        m = latest_metrics(session, ep.id) or {}
        rows.append(f"| {ep.number} | {ep.title} | {ep.hook} | " + " | ".join(str(m.get(c, "")) for c in cols) + " |")
    return "\n".join(rows)


def run_analysis(session: Session, user_id: int, runner: Optional[Runner] = None) -> dict:
    runner = runner or get_runner()
    today = schedule.local_today().isoformat()
    digests, made = [], 0
    for series in session.scalars(select(Series).where(Series.user_id == user_id, Series.status == SeriesStatus.ACTIVE)):
        live = [e for e in series.episodes if e.state == EpisodeState.LIVE and latest_metrics(session, e.id)]
        if len(live) < MIN_LIVE_FOR_ANALYSIS:
            continue
        parts = []
        for target in ("bible", "learnings", "rubric", "script"):
            _, ref = _series_doc(session, series, target)
            parts.append(f"<{target}>\n{ref.content if ref else ''}\n</{target}>")
        voice = documents.read(session, ScopeType.USER, user_id, DocKind.VOICE)
        ctx = (f"# Analysis: {series.name} ({today})\n\n<profile>\n{voice.content if voice else ''}\n</profile>\n\n"
               + "\n\n".join(parts) + f"\n\n<episodes>\n{_metrics_table(session, live)}\n</episodes>\n")
        anchor = live[-1]
        try:
            run = run_step(session, anchor, "analysis", f"analysis-{today}",
                           lambda: (StepRequest(prompt=P_ANALYSIS.format(cap=LEARNINGS_CAP), files={"context.md": ctx},
                                                output_file="output.json"), {"stage": "analysis", "series": series.slug}),
                           runner)
        except (StepPaused, StepFailed) as exc:
            digests.append(f"## {series.name}\n\nAnalysis didn't run: {exc}")
            continue
        try:
            out = extract_json(run.output_text or "")
        except ValueError:
            out = {"digest": run.output_text or "", "proposals": []}
        digests.append(f"## {series.name}\n\n{out.get('digest', '').strip()}")
        for p in out.get("proposals") or []:
            if _make_proposal(session, series, p.get("target", ""), p.get("new_content", ""), p.get("rationale", ""),
                              VersionSource.ANALYSIS):
                made += 1
    if digests:
        text = f"# Insights {today}\n\n" + "\n\n".join(digests)
        documents.write(session, user_id=user_id, scope_type=ScopeType.USER, scope_id=user_id, kind=DocKind.DIGEST,
                        content=text, source=VersionSource.ANALYSIS)
        schedule.open_task(session, user_id=user_id, task_type=TaskType.READ_INSIGHTS,
                           payload={"summary": f"{len(digests)} series, {made} suggested change(s)"})
    session.flush()
    return {"series": len(digests), "proposals": made}


def maybe_distil(session: Session, series: Series, runner: Optional[Runner] = None) -> Optional[Proposal]:
    """Every DISTIL_EVERY approved scripts, propose learnings from the creator's edits."""
    approved = [e for e in series.episodes if (e.meta or {}).get("script_approved_at")]
    done = session.scalar(select(func.count(StageRun.id)).where(
        StageRun.episode_id.in_([e.id for e in series.episodes] or [-1]), StageRun.stage == "distil",
        StageRun.status == RunStatus.SUCCEEDED)) or 0
    if len(approved) < DISTIL_EVERY * (done + 1):
        return None
    pairs = []
    for ep in approved[-DISTIL_EVERY * 2:]:
        doc = documents.find(session, ScopeType.EPISODE, ep.id, DocKind.SCRIPT)
        if doc is None:
            continue
        versions = list(session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
                                        .order_by(DocumentVersion.version)))
        drafted = [v for v in versions if v.source == VersionSource.PIPELINE]
        edited = [v for v in versions if v.source == VersionSource.USER]
        if drafted and edited:
            pairs.append(f"## Ep {ep.number}\n<drafted>\n{drafted[-1].content}</drafted>\n<approved>\n{edited[-1].content}</approved>")
    if len(pairs) < 2:
        return None
    _, learn = _series_doc(session, series, "learnings")
    ctx = f"<learnings>\n{learn.content if learn else ''}\n</learnings>\n\n" + "\n\n".join(pairs)
    runner = runner or get_runner()
    anchor = approved[-1]
    try:
        run = run_step(session, anchor, "distil", f"distil-{done + 1}",
                       lambda: (StepRequest(prompt=P_DISTIL.format(cap=LEARNINGS_CAP), files={"context.md": ctx},
                                            output_file="output.json"), {"stage": "distil", "pairs": len(pairs)}),
                       runner)
        out = extract_json(run.output_text or "")
    except (StepPaused, StepFailed, ValueError):
        return None
    return _make_proposal(session, series, "learnings", out.get("learnings", ""), out.get("rationale", ""),
                          VersionSource.DISTIL)


def accept_proposal(session: Session, prop: Proposal, edited_content: Optional[str] = None) -> dict:
    if prop.status != ProposalStatus.PENDING:
        raise ValueError("proposal already decided")
    doc = session.get(documents.Document, prop.document_id)
    stale = doc.current_version != prop.base_version
    ref, _ = documents.write(session, user_id=prop.user_id, scope_type=doc.scope_type, scope_id=doc.scope_id,
                             kind=doc.kind, key=doc.key, content=edited_content or prop.proposed_content,
                             source=prop.source, note=f"proposal {prop.id}")
    prop.status = ProposalStatus.ACCEPTED
    prop.decided_at = datetime.now(timezone.utc)
    _close_proposal_task(session, prop)
    return {"version": ref.version, "was_stale": stale}


def reject_proposal(session: Session, prop: Proposal) -> None:
    prop.status = ProposalStatus.REJECTED
    prop.decided_at = datetime.now(timezone.utc)
    _close_proposal_task(session, prop)


def _close_proposal_task(session: Session, prop: Proposal) -> None:
    from .models import Task, TaskStatus

    for t in session.scalars(select(Task).where(Task.type == TaskType.REVIEW_PROPOSAL, Task.status == TaskStatus.OPEN)):
        if (t.payload or {}).get("proposal_id") == prop.id:
            t.status = TaskStatus.DONE
            t.completed_at = datetime.now(timezone.utc)
