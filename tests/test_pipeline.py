import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ToneScorer, sample_package, scripted_handler
from suite import documents, integrations, pipeline, schedule
from suite.audio import split_lines
from suite.models import (
    DocKind,
    EpisodeState,
    MediaKind,
    RunStatus,
    ScopeType,
    SeriesStatus,
    StageRun,
    Task,
    TaskStatus,
    TaskType,
)
from suite.package import get_series, save_package
from suite.runner import FakeRunner, RunnerError, UsageLimitError
from suite.users import current_user_id

pytestmark = pytest.mark.ffmpeg


def _setup(db, **pkg):
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(**pkg), mode="create")
        get_series(s, uid, "ballot-math").status = SeriesStatus.ACTIVE
    return uid


def _open_types(s, ep):
    return sorted(t.type.value for t in s.query(Task).filter_by(episode_id=ep.id, status=TaskStatus.OPEN))


def test_full_episode_lifecycle(db, take, monkeypatch, tmp_path):
    uid = _setup(db)
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        ep = get_series(s, uid, "ballot-math").episodes[0]

        res = pipeline.advance(s, ep, runner)
        assert res.state == "script_review" and res.error is None
        steps = [r.step for r in s.query(StageRun).filter_by(episode_id=ep.id).order_by(StageRun.id)]
        assert steps == ["research", "draft", "critic-1", "revise-1", "critic-2"]
        assert documents.read(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT).content.startswith("Line one revised")
        assert _open_types(s, ep) == ["approve_script"]

        # advancing again at a gate does nothing and calls no Claude
        n = len(runner.calls)
        pipeline.advance(s, ep, runner)
        assert len(runner.calls) == n

        edited = "Line one edited\nLine two\nLine three\nLine four"
        pipeline.approve_script(s, ep, edited)
        assert _open_types(s, ep) == ["record"]
        pipeline.attach_recording(s, ep, take)
        res = pipeline.advance(s, ep, runner, scorer=ToneScorer(split_lines(edited)))
        assert res.state == "audio_review"
        align = json.loads(documents.read(s, ScopeType.EPISODE, ep.id, DocKind.ALIGNMENT).content)
        assert [l["line"] for l in align["lines"]] == [0, 1, 2]
        assert align["missing_lines"] == [3]
        assert align["kept_spans"][0][0] > 1.0  # the fumbled first take of line 0 was dropped
        assert _open_types(s, ep) == ["check_audio"]

        pipeline.approve_audio(s, ep, cuts=[(0.2, 0.4)])
        res = pipeline.advance(s, ep, runner)
        assert res.state == "video_review", res
        video = pipeline.media.latest(s, ep.id, MediaKind.VIDEO)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                              str(pipeline.media.local_path(video))], capture_output=True, text=True).stdout
        assert "1080,1920" in out

        pipeline.approve_video(s, ep)
        res = pipeline.advance(s, ep, runner)
        assert res.state == "publishing"
        assert _open_types(s, ep) == ["publish"]  # SuperProfile not configured → manual task
        cont = documents.read(s, ScopeType.SERIES, ep.series_id, DocKind.CONTINUITY).content
        assert "## Ep 1" in cont
        copy = json.loads(documents.read(s, ScopeType.EPISODE, ep.id, DocKind.AUTOMATION_OUTPUT, "post-copy").content)
        assert "FPTP" in copy["caption"]  # keyword CTA enforced
        pipeline.mark_published(s, ep, {"manual": True})
        assert ep.state == EpisodeState.LIVE and _open_types(s, ep) == []


def _drive_to_publishing(s, ep, runner, take):
    pipeline.advance(s, ep, runner)
    pipeline.approve_script(s, ep)
    pipeline.attach_recording(s, ep, take)
    pipeline.advance(s, ep, runner, scorer=ToneScorer(split_lines(
        documents.read(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT).content)))
    pipeline.approve_audio(s, ep)
    pipeline.advance(s, ep, runner)
    pipeline.approve_video(s, ep)


def test_superprofile_publish(db, take, monkeypatch):
    uid = _setup(db)
    monkeypatch.setenv("REELD_PUBLIC_BASE_URL", "https://reeld.example.com")
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        integrations.put(s, uid, "superprofile", {"enabled": True})
        ep = get_series(s, uid, "ballot-math").episodes[0]
        _drive_to_publishing(s, ep, runner, take)
        res = pipeline.advance(s, ep, runner)
        assert res.state == "live", res
        pub = runner.calls[-1]
        assert "mcp__superprofile" in pub.allowed_tools
        assert "https://reeld.example.com/api/suite/media/" in pub.prompt and "FPTP" in pub.prompt
        assert ep.meta["published"]["post_id"] == "p1"


def test_after_slot_waits_for_earlier_episode(db, take):
    uid = _setup(db)
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        ep3 = get_series(s, uid, "ballot-math").episodes[2]  # slot: after Ep 2
        _drive_to_publishing(s, ep3, runner, take)
        res = pipeline.advance(s, ep3, runner)
        assert res.state == "publishing" and _open_types(s, ep3) == []  # waiting, no task yet


def test_usage_limit_pauses_then_resumes_session(db, monkeypatch):
    uid = _setup(db)
    hits = {"n": 0}

    def limit_once(req):
        if "strict reviewer" in req.prompt and hits["n"] == 0:
            hits["n"] += 1
            raise UsageLimitError("hit your limit", datetime.now(timezone.utc) + timedelta(hours=2), "sess-42")
        return None

    runner = FakeRunner(scripted_handler({"strict reviewer": limit_once, "interrupted": lambda r: json.dumps(
        {"pass": True, "results": [], "summary": "ok"})}))
    with db.session_scope() as s:
        ep = get_series(s, uid, "ballot-math").episodes[0]
        res = pipeline.advance(s, ep, runner)
        assert res.paused_until and res.state == "scripting"
        assert pipeline.claude_paused_until(s) is not None
        run = s.query(StageRun).filter_by(episode_id=ep.id, step="critic-1").one()
        assert run.status == RunStatus.PAUSED and run.claude_session_id == "sess-42"

        # still paused: no Claude call is made for any episode
        other = get_series(s, uid, "ballot-math").episodes[1]
        n = len(runner.calls)
        assert pipeline.advance(s, other, runner).paused_until
        assert len(runner.calls) == n
        assert pipeline.resumable_runs(s) == []

        # after the reset the same step resumes its Claude session
        run.resume_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.commit()
        assert [r.id for r in pipeline.resumable_runs(s)] == [run.id]
        res = pipeline.advance(s, ep, runner)
        assert res.state == "script_review", res
        resumed = next(c for c in runner.calls if c.resume_session_id)
        assert resumed.resume_session_id == "sess-42" and "interrupted" in resumed.prompt
        steps = [r.step for r in s.query(StageRun).filter_by(episode_id=ep.id).order_by(StageRun.id)]
        assert steps.count("research") == 1 and steps.count("draft") == 1  # finished steps never reran


def test_failure_raises_task_and_retry(db):
    uid = _setup(db)
    fail = {"on": True}

    def boom(req):
        if fail["on"]:
            raise RunnerError("model refused", "s1")
        return None

    runner = FakeRunner(scripted_handler({"script following": boom}))
    with db.session_scope() as s:
        ep = get_series(s, uid, "ballot-math").episodes[0]
        res = pipeline.advance(s, ep, runner)
        assert res.error and "model refused" in res.error
        assert _open_types(s, ep) == ["fix_failure"]
        assert ep.id not in [e.id for e in pipeline.episodes_to_advance(s)]  # failed episodes wait for a retry
        fail["on"] = False
        pipeline.retry(s, ep)
        res = pipeline.advance(s, ep, runner)
        assert res.state == "script_review"
        run = s.query(StageRun).filter_by(episode_id=ep.id, step="draft").order_by(StageRun.id.desc()).first()
        assert run.attempt == 2 and run.status == RunStatus.SUCCEEDED


def test_script_feedback_and_redo_video(db, take):
    uid = _setup(db)
    runner = FakeRunner(scripted_handler({"creator's note": lambda r: "Line one shorter\nLine two\nLine three\nLine four"}))
    with db.session_scope() as s:
        ep = get_series(s, uid, "ballot-math").episodes[0]
        pipeline.advance(s, ep, runner)
        res = pipeline.script_feedback(s, ep, "make line one shorter", runner)
        assert res.error is None
        assert documents.read(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT).content.startswith("Line one shorter")
        assert "make line one shorter" in runner.calls[-1].files["context.md"]
        pipeline.approve_script(s, ep)
        pipeline.attach_recording(s, ep, take)
        pipeline.advance(s, ep, runner, scorer=ToneScorer(split_lines(
            documents.read(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT).content)))
        pipeline.approve_audio(s, ep)
        pipeline.advance(s, ep, runner)
        n_visual = sum("Design the visuals" in c.prompt for c in runner.calls)
        pipeline.redo_video(s, ep, "bigger text")
        res = pipeline.advance(s, ep, runner)
        assert res.state == "video_review"
        assert sum("Design the visuals" in c.prompt for c in runner.calls) == n_visual + 1
        assert "bigger text" in runner.calls[-1].files["context.md"]


def test_git_automation_commits_and_pushes(db, take, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "site"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / "README.md").write_text("site\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD"], check=True)

    uid = _setup(db, automations=[{"name": "blog", "order": 1, "config": {
        "handler": "git", "repo_path": str(repo), "path": "case-studies/{series}-ep{number}.md",
        "url": "https://armedjuror.in/{path}"}}])
    runner = FakeRunner(scripted_handler())
    with db.session_scope() as s:
        ep = get_series(s, uid, "ballot-math").episodes[0]
        _drive_to_publishing(s, ep, runner, take)
        pipeline.advance(s, ep, runner)
        res = ep.meta["automation_results"]["blog"]
        assert res["path"] == "case-studies/ballot-math-ep1.md" and res["pushed"]
        assert ep.meta["links"]["blog"] == "https://armedjuror.in/case-studies/ballot-math-ep1.md"
        log = subprocess.run(["git", "-C", str(remote), "log", "--oneline", "-1"], capture_output=True, text=True).stdout
        assert "Ep 1" in log
        post_copy_call = next(c for c in runner.calls if "post copy" in c.prompt)
        assert "armedjuror.in/case-studies" in post_copy_call.files["context.md"]


def test_episodes_to_advance_respects_ahead_limit(db, monkeypatch):
    uid = _setup(db)
    monkeypatch.setattr(pipeline, "SCRIPTS_AHEAD", 2)
    with db.session_scope() as s:
        picked = pipeline.episodes_to_advance(s)
        assert [e.number for e in picked] == [1, 2]  # earliest precise slot first
        get_series(s, uid, "ballot-math").status = SeriesStatus.PAUSED
        assert pipeline.episodes_to_advance(s) == []
