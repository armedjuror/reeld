import pytest

from conftest import sample_package, scripted_handler
from suite import runner as runner_mod
from suite.models import Episode, SeriesStatus
from suite.package import get_series, save_package
from suite.runner import FakeRunner
from suite.users import current_user_id


def test_tick_advances_active_series_eagerly(db, monkeypatch):
    import suite.worker as worker

    monkeypatch.setattr(worker, "EAGER", True)
    worker.app.conf.task_always_eager = True
    runner_mod.set_runner(FakeRunner(scripted_handler()))
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(), mode="create")
    assert worker.tick()["queued"] == []  # draft series: nothing moves
    with db.session_scope() as s:
        get_series(s, current_user_id(s), "ballot-math").status = SeriesStatus.ACTIVE
    queued = worker.tick()["queued"]
    assert len(queued) == 3
    with db.session_scope() as s:
        states = {e.number: e.state.value for e in s.query(Episode)}
    assert states == {1: "script_review", 2: "script_review", 3: "script_review"}


def test_daily_without_meta(db, monkeypatch):
    import suite.worker as worker

    monkeypatch.setattr(worker, "EAGER", True)
    with db.session_scope() as s:
        uid = current_user_id(s)
    out = worker.daily(uid)
    assert out["pull"]["skipped"] == "meta not connected" and out["distilled"] == []
