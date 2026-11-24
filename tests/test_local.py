"""Integration tests that need the real world. Run on the Mac:

    REELD_LOCAL_TESTS=1 python3 -m pytest tests/test_local.py -v -s

Each test skips itself if its dependency (claude login, Redis, torch,
macOS `say`, SuperProfile, a Meta token) isn't available.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, ToneScorer, sample_package
from suite import documents, pipeline
from suite.audio import split_lines
from suite.models import DocKind, EpisodeState, MediaKind, ScopeType, SeriesStatus
from suite.package import SeriesPackage, get_series, save_package
from suite.runner import ClaudeCLIRunner
from suite.users import current_user_id

pytestmark = pytest.mark.local


def _need(binary):
    if not shutil.which(binary):
        pytest.skip(f"{binary} not installed")


def _tiny_package():
    return SeriesPackage.model_validate({
        "slug": "smoke", "name": "Smoke test", "status": "active",
        "bible": "## Audience\n\nCurious adults.\n\n## Length\n\nAbout 15 seconds, 3 short spoken lines.\n\n## Language\n\nEnglish.\n",
        "plan": [{"number": 1, "title": "Why the sky is blue", "core_idea": "Rayleigh scattering favours short wavelengths",
                  "hook": "The sky isn't blue for the reason you think", "keyword": "SKY", "slot": {"kind": "unscheduled"}}],
        "skills": {
            "script": "Write exactly 3 short spoken lines, plain English, under 12 words each.",
            "rubric": "- Exactly 3 spoken lines\n- Each line under 12 words\n- First line is the hook\n- Mentions scattering\n"
                      "- Last line asks viewers to comment SKY\n- No hashtags in the script",
            "visuals": "Two or three scenes. Dark blue background, large white sans-serif text, one simple shape each.",
            "auto-note": "Write a 3-sentence plain-text explainer of the episode's idea.",
        },
        "automations": [{"name": "note", "order": 1}],
    })


def test_real_claude_dummy_episode(db, take):
    """One tiny episode end to end with real `claude -p` (costs a few cents)."""
    _need("claude")
    _need("ffmpeg")
    runner = ClaudeCLIRunner(timeout_s=600)
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, _tiny_package(), mode="create")
        ep = get_series(s, uid, "smoke").episodes[0]

        res = pipeline.advance(s, ep, runner)
        assert res.state == "script_review", res
        script = documents.read(s, ScopeType.EPISODE, ep.id, DocKind.SCRIPT).content
        critic = json.loads(documents.read(s, ScopeType.EPISODE, ep.id, DocKind.CRITIC).content)
        print("\nSCRIPT:\n" + script + "\nCRITIC:", critic.get("summary"))
        assert 2 <= len(split_lines(script)) <= 6 and "results" in critic

        pipeline.approve_script(s, ep)
        pipeline.attach_recording(s, ep, take)
        lines = split_lines(script)
        scorer = ToneScorer((lines + ["", "", ""])[:3])
        assert pipeline.advance(s, ep, runner, scorer=scorer).state == "audio_review"
        pipeline.approve_audio(s, ep)
        res = pipeline.advance(s, ep, runner)
        assert res.state == "video_review", res
        video = pipeline.media.latest(s, ep.id, MediaKind.VIDEO)
        print("VIDEO:", pipeline.media.local_path(video))

        pipeline.approve_video(s, ep)
        res = pipeline.advance(s, ep, runner)
        assert res.state == "publishing", res
        copy = json.loads(documents.read(s, ScopeType.EPISODE, ep.id, DocKind.AUTOMATION_OUTPUT, "post-copy").content)
        assert "SKY" in copy["caption"]
        assert documents.read(s, ScopeType.EPISODE, ep.id, DocKind.AUTOMATION_OUTPUT, "note").content.strip()
        assert "## Ep 1" in documents.read(s, ScopeType.SERIES, ep.series_id, DocKind.CONTINUITY).content


def test_reeld_mcp_from_claude(db, tmp_path):
    """Claude Code can reach the reeld MCP server and read the series list."""
    _need("claude")
    with db.session_scope() as s:
        save_package(s, current_user_id(s), sample_package(), mode="create")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"reeld": {
        "command": sys.executable, "args": ["-m", "suite.mcp_server"],
        "env": {"DATABASE_URL": os.environ["DATABASE_URL"], "PYTHONPATH": str(ROOT)}}}}))
    out = subprocess.run(
        ["claude", "-p", "Call the reeld list_series tool and reply with only the slugs, comma-separated.",
         "--output-format", "json", "--mcp-config", str(cfg), "--strict-mcp-config",
         "--allowedTools", "mcp__reeld"],
        capture_output=True, text=True, timeout=300, cwd=ROOT)
    result = json.loads(out.stdout)
    assert not result.get("is_error"), result
    assert "ballot-math" in result["result"]


def test_celery_worker_over_redis(db, take, tmp_path):
    """A real worker picks a job off Redis and advances a non-Claude stage."""
    _need("redis-server")
    redis_dir = tmp_path / "redis"
    redis_dir.mkdir()
    port = "6391"
    redis = subprocess.Popen(["redis-server", "--port", port, "--dir", str(redis_dir), "--save", ""],
                             stdout=subprocess.DEVNULL)
    env = {**os.environ, "REDIS_URL": f"redis://localhost:{port}/0", "REELD_EAGER": "0",
           "REELD_MEDIA_ROOT": str(pipeline.media.MEDIA_ROOT), "PYTHONPATH": str(ROOT)}
    worker = subprocess.Popen([sys.executable, "-m", "celery", "-A", "suite.worker", "worker", "--pool", "solo",
                               "--loglevel", "warning"], env=env, cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with db.session_scope() as s:
            uid = current_user_id(s)
            save_package(s, uid, sample_package(), mode="create")
            ep = get_series(s, uid, "ballot-math").episodes[0]
            documents.write(s, user_id=uid, scope_type=ScopeType.EPISODE, scope_id=ep.id, kind=DocKind.SCRIPT,
                            content="Line one\nLine two\nLine three", source=documents.VersionSource.USER)
            ep.state = EpisodeState.AWAITING_RECORDING
            pipeline.attach_recording(s, ep, take)
            eid = ep.id
        code = (f"import suite.worker as w; w.app.conf.task_always_eager=False; "
                f"print(w.broker_ok()); w.advance_episode.delay({eid})")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert out.stdout.strip().startswith("True"), out.stderr
        for _ in range(60):
            with db.session_scope() as s:
                state = s.get(pipeline.Episode, eid).state
            if state == EpisodeState.AUDIO_REVIEW:
                break
            time.sleep(1)
        assert state == EpisodeState.AUDIO_REVIEW
    finally:
        worker.terminate()
        redis.terminate()


def test_mms_alignment_drops_fumbled_take(tmp_path):
    """Forced alignment on real speech (macOS `say`): the fumbled first take is dropped."""
    _need("say")
    _need("ffmpeg")
    pytest.importorskip("torchaudio")
    from suite.audio import MMSScorer, process_take

    lines = ["The sky looks blue in the afternoon.", "Sunlight scatters off tiny air molecules.",
             "Comment sky and I will send you more."]
    parts = []
    for k, text in enumerate([lines[0][:14], lines[0], lines[1], lines[2]]):
        aiff = tmp_path / f"p{k}.aiff"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        parts.append(aiff)
    listing = tmp_path / "list.txt"
    gap = tmp_path / "gap.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.9", str(gap)], check=True)
    wavs = []
    for k, p in enumerate(parts):
        w = tmp_path / f"p{k}.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(p), "-ar", "22050", "-ac", "1", str(w)], check=True)
        wavs += [w, gap]
    listing.write_text("".join(f"file '{w}'\n" for w in wavs))
    take = tmp_path / "take.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), str(take)], check=True)

    res = process_take(take, "\n".join(lines), tmp_path / "clean.m4a", scorer=MMSScorer())
    print(json.dumps(res, indent=1))
    assert res["method"] == "forced-alignment"
    assert [l["line"] for l in res["lines"]] == [0, 1, 2]
    assert res["missing_lines"] == []
    first_kept = res["kept_spans"][0][0]
    assert first_kept > 1.0  # the partial first take (≈1 s) was not kept


def test_superprofile_tools_visible():
    """SuperProfile is registered in Claude Code and signed in."""
    _need("claude")
    if os.environ.get("REELD_SUPERPROFILE") != "1":
        pytest.skip("set REELD_SUPERPROFILE=1 after `claude mcp add ... superprofile` and signing in")
    server = os.environ.get("REELD_SUPERPROFILE_SERVER", "superprofile")
    out = subprocess.run(["claude", "-p", f"List the names of the {server} MCP tools you can use, one per line.",
                          "--output-format", "json", "--allowedTools", f"mcp__{server}"],
                         capture_output=True, text=True, timeout=300)
    result = json.loads(out.stdout)
    assert not result.get("is_error"), result
    assert "dm" in result["result"].lower() or "automation" in result["result"].lower()


def test_meta_token_live():
    """Your Meta token can list reels and read insights."""
    token = os.environ.get("REELD_META_TOKEN")
    if not token:
        pytest.skip("set REELD_META_TOKEN to a long-lived Instagram token")
    from suite.insights import MetaClient

    c = MetaClient(token)
    print(c.me())
    media = c.list_media(limit=5)
    assert isinstance(media, list)
    reels = [m for m in media if m.get("media_product_type") == "REELS"]
    if reels:
        metrics = c.media_insights(reels[0]["id"])
        print(metrics)
        assert "views" in metrics or "reach" in metrics
