import json
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest

from conftest import sample_package, scripted_handler
from suite import documents, insights, integrations, schedule
from suite.models import (
    DocKind,
    DocumentVersion,
    EpisodeState,
    InsightSnapshot,
    Proposal,
    ProposalStatus,
    ScopeType,
    SeriesStatus,
    SlotKind,
    TaskStatus,
    TaskType,
    VersionSource,
)
from suite.package import get_series, save_package
from suite.runner import FakeRunner
from suite.users import current_user_id


def _series(db, **kw):
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(**kw), mode="create")
    return uid


# ─── Schedule ─────────────────────────────────────────────────────────────────


def test_backward_planned_due_dates(db):
    uid = _series(db)
    with db.session_scope() as s:
        ep1 = get_series(s, uid, "ballot-math").episodes[0]  # precise 2026-12-01 19:30 IST
        pub = schedule.publish_at(ep1)
        assert pub == datetime(2026, 12, 1, 14, 0, tzinfo=timezone.utc)
        t = schedule.open_task(s, user_id=uid, task_type=TaskType.APPROVE_SCRIPT, episode=ep1)
        assert t.due_at == pub - timedelta(days=3)
        ep2 = get_series(s, uid, "ballot-math").episodes[1]  # vague week
        assert schedule.due_for(ep2, TaskType.RECORD) is None
        # firming up a vague slot re-plans its open tasks
        t2 = schedule.open_task(s, user_id=uid, task_type=TaskType.RECORD, episode=ep2)
        ep2.slot_kind, ep2.slot_date, ep2.slot_time = SlotKind.PRECISE, date(2026, 12, 10), time(18, 0)
        schedule.refresh_due_dates(s, ep2)
        assert t2.due_at == datetime(2026, 12, 8, 12, 30, tzinfo=timezone.utc)


def test_task_list_order_grouping_and_overdue(db):
    uid = _series(db)
    with db.session_scope() as s:
        e1, e2, e3 = get_series(s, uid, "ballot-math").episodes
        schedule.open_task(s, user_id=uid, task_type=TaskType.RECORD, episode=e2)
        schedule.open_task(s, user_id=uid, task_type=TaskType.RECORD, episode=e3)
        schedule.open_task(s, user_id=uid, task_type=TaskType.APPROVE_SCRIPT, episode=e1)
        old = schedule.open_task(s, user_id=uid, task_type=TaskType.FIX_FAILURE, episode=e3)
        old.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
        views = schedule.task_list(s, uid)
        assert [v.type for v in views] == ["fix_failure", "approve_script", "record"]
        assert views[0].overdue
        assert views[2].title == "Record 2 episodes" and len(views[2].group) == 2
        # same task type for the same episode is refreshed, not duplicated
        schedule.open_task(s, user_id=uid, task_type=TaskType.APPROVE_SCRIPT, episode=e1)
        assert len(schedule.task_list(s, uid)) == 3


def test_day_strip_and_calendar(db):
    uid = _series(db)
    with db.session_scope() as s:
        strip = schedule.day_strip(s, uid, date(2026, 12, 1))
        assert [(e["number"], e["time"]) for e in strip] == [(1, "19:30")]
        cal = schedule.calendar(s, uid, date(2026, 11, 30), date(2026, 12, 13))
        kinds = {(c["number"], c["kind"]) for c in cal}
        assert kinds == {(1, "precise"), (2, "week"), (3, "after")}
        week = next(c for c in cal if c["kind"] == "week")
        assert (week["start"], week["end"]) == ("2026-12-07", "2026-12-13")


# ─── Meta client ──────────────────────────────────────────────────────────────


def _meta_transport(calls):
    def handle(req: httpx.Request):
        calls.append(str(req.url))
        path = req.url.path
        if path.endswith("/refresh_access_token"):
            return httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000})
        if path.endswith("/me/media"):
            if "after" in str(req.url):
                return httpx.Response(200, json={"data": [
                    {"id": "m2", "caption": "Old post", "media_product_type": "REELS", "timestamp": "2026-01-01T00:00:00+0000"}]})
            return httpx.Response(200, json={"data": [
                {"id": "m1", "caption": "Why two parties?\nComment FPTP for the case study", "media_product_type": "REELS",
                 "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"), "permalink": "https://ig/r/1"}],
                "paging": {"next": "https://graph.instagram.com/v27.0/me/media?after=X"}})
        if path.endswith("/insights"):
            metric = req.url.params.get("metric")
            if "," in metric:
                return httpx.Response(400, json={"error": {"message": "metric reels_skip_rate is in development"}})
            if metric == "reels_skip_rate":
                return httpx.Response(400, json={"error": {"message": "unsupported"}})
            return httpx.Response(200, json={"data": [{"name": metric, "period": "lifetime", "values": [{"value": 10}]}]})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return httpx.MockTransport(handle)


def test_meta_pull_links_and_snapshots(db):
    uid = _series(db)
    calls = []
    with db.session_scope() as s:
        integrations.put(s, uid, "meta", {"access_token": "OLD",
                                          "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()})
        ep = get_series(s, uid, "ballot-math").episodes[0]
        ep.state = EpisodeState.LIVE
        ep.meta = {"published": {"at": datetime.now(timezone.utc).isoformat()}}
        client = insights.client_for(s, uid, transport=_meta_transport(calls))
        assert client.token == "NEW" and integrations.get(s, uid, "meta")["access_token"] == "NEW"
        res = insights.pull_insights(s, uid, client=client)
        assert res == {"media": 2, "linked": 1, "snapshots": 1}
        assert ep.meta["instagram_media_id"] == "m1"
        m = insights.latest_metrics(s, ep.id)
        assert m["views"] == 10 and "reels_skip_rate" not in m
        assert insights.pull_insights(s, uid, client=client)["skipped"] == "pulled recently"


def test_meta_not_connected(db):
    with db.session_scope() as s:
        assert insights.pull_insights(s, current_user_id(s))["skipped"] == "meta not connected"


# ─── Analysis, proposals, distillation ────────────────────────────────────────


def _live_with_stats(s, series, n):
    for ep in series.episodes[:n]:
        ep.state = EpisodeState.LIVE
        s.add(InsightSnapshot(episode_id=ep.id, metrics={"views": 100 * ep.number, "ig_reels_avg_watch_time": 9000}))
    s.flush()


def test_analysis_needs_three_live_reels(db):
    uid = _series(db)
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        sr = get_series(s, uid, "ballot-math")
        sr.status = SeriesStatus.ACTIVE
        _live_with_stats(s, sr, 2)
        assert insights.run_analysis(s, uid, runner) == {"series": 0, "proposals": 0}
        assert runner.calls == []


def test_analysis_digest_proposal_accept(db):
    uid = _series(db)
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        sr = get_series(s, uid, "ballot-math")
        sr.status = SeriesStatus.ACTIVE
        _live_with_stats(s, sr, 3)
        res = insights.run_analysis(s, uid, runner)
        assert res == {"series": 1, "proposals": 1}
        assert "| 3 | Caste maths" in runner.calls[0].files["context.md"]
        digest = documents.read(s, ScopeType.USER, uid, DocKind.DIGEST).content
        assert "Question hooks win." in digest
        types = sorted(t.type.value for t in s.query(insights.schedule.Task).filter_by(status=TaskStatus.OPEN))
        assert types == ["read_insights", "review_proposal"]
        prop = s.query(Proposal).one()
        # nothing applied until accepted
        assert documents.read(s, ScopeType.SERIES, sr.id, DocKind.LEARNINGS).content == ""
        out = insights.accept_proposal(s, prop)
        assert out["version"] == 2 and not out["was_stale"]
        assert documents.read(s, ScopeType.SERIES, sr.id, DocKind.LEARNINGS).content.startswith("- Open with a question")
        assert prop.status == ProposalStatus.ACCEPTED
        assert not s.query(insights.schedule.Task).filter_by(type=TaskType.REVIEW_PROPOSAL, status=TaskStatus.OPEN).count()
        with pytest.raises(ValueError):
            insights.accept_proposal(s, prop)


def test_distil_every_n_approvals(db, monkeypatch):
    monkeypatch.setattr(insights, "DISTIL_EVERY", 2)
    uid = _series(db)
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        sr = get_series(s, uid, "ballot-math")
        assert insights.maybe_distil(s, sr, runner) is None  # no approvals yet
        for ep in sr.episodes[:2]:
            for content, src in (("Long line here\n", VersionSource.PIPELINE), ("Short\n", VersionSource.USER)):
                documents.write(s, user_id=uid, scope_type=ScopeType.EPISODE, scope_id=ep.id, kind=DocKind.SCRIPT,
                                content=content, source=src)
            ep.meta = {"script_approved_at": "2026-11-20T00:00:00+00:00"}
        prop = insights.maybe_distil(s, sr, runner)
        assert prop is not None and prop.source == VersionSource.DISTIL
        assert "<drafted>" in runner.calls[0].files["context.md"]
        assert insights.maybe_distil(s, sr, runner) is None  # already distilled for this batch
