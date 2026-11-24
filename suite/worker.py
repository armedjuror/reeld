"""Celery worker (spec §2, §10).

    celery -A suite.worker worker --beat --concurrency 2 --loglevel info

Requires Redis (REDIS_URL, default redis://localhost:6379/0). Set
REELD_EAGER=1 to run everything inline without Redis (dev and tests).

Beat runs `tick` every few minutes: it resumes steps paused by a usage limit
once their reset time passes and advances every episode that can move.
Episodes are locked while advancing, so two workers never run the same one.
"""

from __future__ import annotations

import os
from datetime import timedelta

from celery import Celery
from sqlalchemy import select

from .db import session_scope
from .models import Episode, User

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
EAGER = os.environ.get("REELD_EAGER") == "1"
TICK_SECONDS = int(os.environ.get("REELD_TICK_SECONDS", "300"))

app = Celery("reeld", broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_always_eager=EAGER,
    task_eager_propagates=True,
    task_acks_late=True,  # a crashed worker's job is redelivered
    worker_prefetch_multiplier=1,
    timezone=os.environ.get("REELD_TZ", "Asia/Kolkata"),
    beat_schedule={"tick": {"task": "suite.worker.tick", "schedule": timedelta(seconds=TICK_SECONDS)}},
)

_LOCK_TTL = 60 * 60  # an advance can run long Claude steps


def _lock(key: str):
    """Redis lock, or a no-op lock when running eagerly."""
    if EAGER:
        from contextlib import nullcontext

        return nullcontext(True)
    import redis

    return redis.Redis.from_url(REDIS_URL).lock(f"reeld:{key}", timeout=_LOCK_TTL, blocking=False)


@app.task(name="suite.worker.advance_episode")
def advance_episode(episode_id: int) -> dict:
    from . import pipeline

    lock = _lock(f"episode:{episode_id}")
    acquired = lock.acquire() if hasattr(lock, "acquire") else True
    if not acquired:
        return {"episode_id": episode_id, "skipped": "already running"}
    try:
        with session_scope() as s:
            ep = s.get(Episode, episode_id)
            if ep is None:
                return {"episode_id": episode_id, "skipped": "not found"}
            res = pipeline.advance(s, ep)
            return res.__dict__
    finally:
        if hasattr(lock, "release"):
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - expired lock
                pass


@app.task(name="suite.worker.tick")
def tick() -> dict:
    """Resume paused steps whose reset passed; advance everything that can move."""
    from . import pipeline

    with session_scope() as s:
        if pipeline.claude_paused_until(s):
            ids: list[int] = []  # still paused: nothing Claude-bound can start
            non_claude = [e.id for e in pipeline.episodes_to_advance(s)
                          if e.state.value == "audio_processing"]
            ids = non_claude
        else:
            resumed = {r.episode_id for r in pipeline.resumable_runs(s)}
            ids = sorted(resumed | {e.id for e in pipeline.episodes_to_advance(s)})
    for eid in ids:
        advance_episode.delay(eid)
    return {"queued": ids}


@app.task(name="suite.worker.daily")
def daily(user_id: int, force: bool = False) -> dict:
    """On app open: pull reel stats, then run the analysis and distil steps."""
    from . import insights

    with session_scope() as s:
        pulled = insights.pull_insights(s, user_id, force=force)
    analysis = {}
    if not pulled.get("skipped") or force:
        with session_scope() as s:
            analysis = insights.run_analysis(s, user_id)
    with session_scope() as s:
        from .models import Series

        distilled = [sr.slug for sr in s.scalars(select(Series).where(Series.user_id == user_id))
                     if insights.maybe_distil(s, sr)]
    return {"pull": pulled, "analysis": analysis, "distilled": distilled}


def broker_ok() -> bool:
    """True when jobs can be queued (eager mode, or Redis answers within 0.5 s)."""
    if EAGER:
        return True
    try:
        import redis

        return bool(redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5).ping())
    except Exception:  # noqa: BLE001
        return False


def kick(episode_id: int) -> bool:
    """Advance an episode in the background after a user action."""
    if not broker_ok():
        return False
    advance_episode.delay(episode_id)
    return True


def all_users() -> list[int]:
    with session_scope() as s:
        return list(s.scalars(select(User.id)))
