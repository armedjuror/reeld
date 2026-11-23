"""Tasks and calendar (spec §7).

The home screen is the user's open tasks. Tasks are raised by the pipeline
at each gate; for episodes with a precise slot, each task's due time is
planned backwards from the publish time:

    script approved  slot − 3 days
    recorded         slot − 2 days
    audio approved   slot − 2 days
    video approved   slot − 1 day
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Episode, EpisodeState, Series, SlotKind, Task, TaskStatus, TaskType

TZ = ZoneInfo(os.environ.get("REELD_TZ", "Asia/Kolkata"))
DEFAULT_PUBLISH_TIME = time(19, 0)  # when a precise slot has no time

LEAD_TIMES: dict[TaskType, timedelta] = {
    TaskType.APPROVE_SCRIPT: timedelta(days=3),
    TaskType.RECORD: timedelta(days=2),
    TaskType.CHECK_AUDIO: timedelta(days=2),
    TaskType.APPROVE_VIDEO: timedelta(days=1),
    TaskType.PUBLISH: timedelta(hours=1),
}
ESTIMATES = {  # minutes, shown on the task card
    TaskType.APPROVE_SCRIPT: 5,
    TaskType.RECORD: 4,
    TaskType.CHECK_AUDIO: 2,
    TaskType.APPROVE_VIDEO: 2,
    TaskType.REVIEW_PROPOSAL: 2,
    TaskType.READ_INSIGHTS: 2,
    TaskType.FIX_FAILURE: 3,
    TaskType.PUBLISH: 3,
}
TYPE_ORDER = {  # tie-break: things later in the pipeline first (closer to publishing)
    TaskType.FIX_FAILURE: 0,
    TaskType.PUBLISH: 1,
    TaskType.APPROVE_VIDEO: 1,
    TaskType.CHECK_AUDIO: 2,
    TaskType.RECORD: 3,
    TaskType.APPROVE_SCRIPT: 4,
    TaskType.REVIEW_PROPOSAL: 5,
    TaskType.READ_INSIGHTS: 6,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def publish_at(ep: Episode) -> Optional[datetime]:
    """Exact publish moment for precise slots, else None (vague or unscheduled)."""
    if ep.slot_kind != SlotKind.PRECISE or ep.slot_date is None:
        return None
    return datetime.combine(ep.slot_date, ep.slot_time or DEFAULT_PUBLISH_TIME, tzinfo=TZ).astimezone(timezone.utc)


def slot_window(ep: Episode) -> Optional[tuple[date, date]]:
    """Calendar band for an episode: one day for precise, a week or a month for vague."""
    d = ep.slot_date
    if d is None:
        return None
    if ep.slot_kind == SlotKind.PRECISE:
        return d, d
    if ep.slot_kind == SlotKind.WEEK:
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    if ep.slot_kind == SlotKind.MONTH:
        start = d.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt - timedelta(days=1)
    return None


def due_for(ep: Optional[Episode], task_type: TaskType) -> Optional[datetime]:
    if ep is None:
        return None
    at = publish_at(ep)
    lead = LEAD_TIMES.get(task_type)
    return at - lead if at and lead else None


def open_task(session: Session, *, user_id: int, task_type: TaskType, episode: Optional[Episode] = None,
              series_id: Optional[int] = None, payload: Optional[dict[str, Any]] = None,
              stage_run_id: Optional[int] = None) -> Task:
    """Create a task, or refresh the existing open one of the same type/episode."""
    q = select(Task).where(Task.user_id == user_id, Task.type == task_type, Task.status == TaskStatus.OPEN)
    q = q.where(Task.episode_id == episode.id) if episode else q.where(Task.episode_id.is_(None))
    if series_id is not None and episode is None:
        q = q.where(Task.series_id == series_id)
    existing = session.scalar(q)
    if existing and task_type not in (TaskType.REVIEW_PROPOSAL,):
        existing.payload = payload or existing.payload
        existing.due_at = due_for(episode, task_type)
        existing.stage_run_id = stage_run_id or existing.stage_run_id
        return existing
    task = Task(
        user_id=user_id,
        series_id=series_id or (episode.series_id if episode else None),
        episode_id=episode.id if episode else None,
        type=task_type,
        status=TaskStatus.OPEN,
        priority=TYPE_ORDER[task_type],
        due_at=due_for(episode, task_type),
        estimate_minutes=ESTIMATES.get(task_type),
        payload=payload or {},
        stage_run_id=stage_run_id,
    )
    session.add(task)
    session.flush()
    return task


def close_tasks(session: Session, episode: Episode, *types: TaskType, status: TaskStatus = TaskStatus.DONE) -> None:
    for t in session.scalars(
        select(Task).where(Task.episode_id == episode.id, Task.status == TaskStatus.OPEN, Task.type.in_(types))
    ):
        t.status = status
        t.completed_at = now_utc()


def refresh_due_dates(session: Session, episode: Episode) -> None:
    """After a slot changes, re-plan the episode's open tasks."""
    for t in session.scalars(select(Task).where(Task.episode_id == episode.id, Task.status == TaskStatus.OPEN)):
        t.due_at = due_for(episode, t.type)


# ─── Reading the task list ────────────────────────────────────────────────────


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sort_key(t: Task):
    due = _as_utc(t.due_at)
    return (due is None, due or datetime.max.replace(tzinfo=timezone.utc), t.priority, t.id)


@dataclass
class TaskView:
    id: int
    type: str
    title: str
    detail: str
    episode_id: Optional[int]
    series: Optional[str]
    due_at: Optional[str]
    overdue: bool
    estimate_minutes: Optional[int]
    payload: dict
    group: Optional[list[int]] = None  # ids folded into this card (batched recording)


def _title(t: Task, ep: Optional[Episode]) -> str:
    label = f"Ep {ep.number}: {ep.title}" if ep else ""
    return {
        TaskType.APPROVE_SCRIPT: f"Approve script · {label}",
        TaskType.RECORD: f"Record · {label}",
        TaskType.CHECK_AUDIO: f"Check audio · {label}",
        TaskType.APPROVE_VIDEO: f"Approve video · {label}",
        TaskType.REVIEW_PROPOSAL: "Review a suggested change",
        TaskType.READ_INSIGHTS: "Read today's insights",
        TaskType.FIX_FAILURE: f"Fix a failed step · {label}",
        TaskType.PUBLISH: f"Publish · {label}",
    }[t.type]


def task_list(session: Session, user_id: int, group_records: bool = True) -> list[TaskView]:
    tasks = sorted(
        session.scalars(select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.OPEN)),
        key=_sort_key,
    )
    eps = {e.id: e for e in session.scalars(
        select(Episode).where(Episode.id.in_([t.episode_id for t in tasks if t.episode_id]))
    )}
    series = {s.id: s for s in session.scalars(select(Series).where(Series.user_id == user_id))}
    now = now_utc()
    views: list[TaskView] = []
    record_card: Optional[TaskView] = None
    for t in tasks:
        ep = eps.get(t.episode_id) if t.episode_id else None
        due = _as_utc(t.due_at)
        view = TaskView(
            id=t.id,
            type=t.type.value,
            title=_title(t, ep),
            detail=(t.payload or {}).get("summary", ""),
            episode_id=t.episode_id,
            series=series[t.series_id].name if t.series_id in series else None,
            due_at=due.isoformat() if due else None,
            overdue=bool(due and due < now),
            estimate_minutes=t.estimate_minutes,
            payload=t.payload or {},
        )
        if group_records and t.type == TaskType.RECORD:
            if record_card is None:
                record_card = view
                record_card.group = [t.id]
                views.append(record_card)
            else:
                record_card.group.append(t.id)
                record_card.estimate_minutes = (record_card.estimate_minutes or 0) + (t.estimate_minutes or 0)
                if view.overdue:
                    record_card.overdue = True
            continue
        views.append(view)
    if record_card and record_card.group and len(record_card.group) > 1:
        record_card.title = f"Record {len(record_card.group)} episodes"
    return views


def day_strip(session: Session, user_id: int, day: date) -> list[dict]:
    """Precise-dated episodes publishing on `day` (local), with what's still needed."""
    out = []
    q = (
        select(Episode, Series)
        .join(Series, Series.id == Episode.series_id)
        .where(Series.user_id == user_id, Episode.slot_kind == SlotKind.PRECISE, Episode.slot_date == day)
        .order_by(Episode.slot_time)
    )
    for ep, s in session.execute(q):
        open_types = [t.type.value for t in session.scalars(
            select(Task).where(Task.episode_id == ep.id, Task.status == TaskStatus.OPEN))]
        out.append({
            "episode_id": ep.id,
            "series": s.name,
            "number": ep.number,
            "title": ep.title,
            "time": (ep.slot_time or DEFAULT_PUBLISH_TIME).strftime("%H:%M"),
            "state": ep.state.value,
            "ready": ep.state in (EpisodeState.PUBLISHING, EpisodeState.LIVE),
            "needs": open_types,
        })
    return out


def calendar(session: Session, user_id: int, start: date, end: date) -> list[dict]:
    """Episodes whose slot window overlaps [start, end]. Vague ones come as bands."""
    out = []
    q = select(Episode, Series).join(Series, Series.id == Episode.series_id).where(Series.user_id == user_id)
    for ep, s in session.execute(q):
        win = slot_window(ep)
        entry = {
            "episode_id": ep.id, "series": s.name, "series_slug": s.slug, "number": ep.number,
            "title": ep.title, "state": ep.state.value, "kind": ep.slot_kind.value,
            "after_episode": ep.after_episode,
        }
        if win is None:
            if ep.slot_kind in (SlotKind.AFTER, SlotKind.UNSCHEDULED):
                out.append({**entry, "start": None, "end": None})
            continue
        if win[1] < start or win[0] > end:
            continue
        out.append({**entry, "start": win[0].isoformat(), "end": win[1].isoformat(),
                    "time": ep.slot_time.strftime("%H:%M") if ep.slot_time else None})
    out.sort(key=lambda e: (e["start"] is None, e["start"] or "", e["series"], e["number"]))
    return out


def local_today() -> date:
    return now_utc().astimezone(TZ).date()
