"""Seed a demo series with episodes at every gate, without calling Claude.

    python3 -m suite.cli demo

Uses a scripted fake runner and generated audio, so it costs nothing and
works offline. The demo series is left paused so the worker won't pick it up.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from . import documents, pipeline, schedule
from .audio import DurationScorer
from .insights import _make_proposal
from .models import (
    DocKind,
    EpisodeState,
    InsightSnapshot,
    ScopeType,
    SeriesStatus,
    VersionSource,
)
from .package import SeriesPackage, get_series, save_package
from .runner import FakeRunner, StepResult
from .users import create_user, current_user_id, UserNotConfigured

SLUG = "demo-ballot-math"
SCRIPT = (
    "Every election, two names. Why only two?\n"
    "Imagine three scientists debating one theory.\n"
    "Two of them agree on almost everything.\n"
    "So they split the vote, and the third one wins.\n"
    "That is the spoiler effect, and it shapes every party.\n"
    "Comment FPTP and I'll send you the full case study."
)


def _svg(colour: str, text: str) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920">'
            f'<rect width="1080" height="1920" fill="{colour}"/>'
            f'<text x="540" y="900" font-family="sans-serif" font-size="92" font-weight="700" '
            f'text-anchor="middle" fill="#fff">{text}</text></svg>').encode()


def fake_runner() -> FakeRunner:
    def handler(req):
        p = req.prompt
        if "strict reviewer" in p:
            return json.dumps({"pass": False, "summary": "One flag left after 3 revisions", "results": [
                {"criterion": "Hook lands in the first 3 seconds", "pass": True, "evidence": "Opens on the question", "fix": ""},
                {"criterion": "One running example throughout", "pass": True, "evidence": "Scientists debate", "fix": ""},
                {"criterion": "Fits the slot length", "pass": False, "evidence": "~80 s", "fix": "Trim the fourth line"},
            ]})
        if "research note" in p:
            return "# Research\n\nDuverger's law... \n\nSources:\n- https://en.wikipedia.org/wiki/Duverger%27s_law"
        if "Revise" in p or "script following" in p:
            return SCRIPT
        if "Design the visuals" in p:
            files = {f"scenes/scene-0{k + 1}.svg": _svg(c, t) for k, (c, t) in
                     enumerate([("#1E6B47", "Only two?"), ("#18221E", "Spoiler effect"), ("#1E6B47", "Comment FPTP")])}
            plan = [{"start": s, "file": f"scenes/scene-0{k + 1}.svg"} for k, s in enumerate([0, 1.2, 2.4])]
            return StepResult(output=json.dumps(plan), session_id="demo", raw={}, files=files)
        if "Run this automation" in p:
            return "# FPTP case study\n\nThe full maths behind the spoiler effect."
        if "post copy" in p:
            return json.dumps({"caption": "Why does every election end with two names?\n\nComment FPTP for the case study.",
                               "hashtags": ["ballotmath", "elections"], "dm_message": "Here's the FPTP case study: https://armedjuror.in"})
        if "continuity entry" in p:
            return "## Ep 1: Why only two parties\n- Covered: spoiler effect\n- Promised: dynasties next"
        return "ok"

    return FakeRunner(handler)


def _tone(path: Path, seconds: float = 6.0) -> None:
    expr = "if(lt(mod(t\\,1.6)\\,1.1)\\,0.4*sin(2*PI*220*t)\\,0)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s=44100:d={seconds}", str(path)], check=True)


def seed(session: Session) -> dict:
    try:
        uid = current_user_id(session)
    except UserNotConfigured:
        uid = create_user(session, "demo@reeld.local", "Demo").id
    if get_series(session, uid, SLUG):
        return {"skipped": "demo series already exists", "slug": SLUG}
    if documents.read(session, ScopeType.USER, uid, DocKind.VOICE) is None:
        documents.write(session, user_id=uid, scope_type=ScopeType.USER, scope_id=uid, kind=DocKind.VOICE,
                        content="## Narration\n\nTalk like explaining to a friend over chai.\n\n## Language\n\nMalayalam voiceover, English captions.\n",
                        source=VersionSource.USER)
    today = schedule.local_today()
    plan = []
    for n, (title, slot) in enumerate([
        ("Why only two parties", {"kind": "precise", "date": today.isoformat(), "time": "19:30"}),
        ("Dynasties under FPTP", {"kind": "precise", "date": (today + timedelta(days=1)).isoformat(), "time": "19:30"}),
        ("Caste maths", {"kind": "week", "date": (today + timedelta(days=7)).isoformat()}),
        ("What the US midterms show", {"kind": "precise", "date": (today - timedelta(days=6)).isoformat(), "time": "19:00"}),
        ("Runoffs, briefly", {"kind": "after", "after_episode": 3}),
    ], start=1):
        plan.append({"number": n, "title": title, "core_idea": f"Idea {n}", "hook": f"Hook {n}",
                     "keyword": ["FPTP", "DYNASTY", "BLOC", "MIDTERM", "RUNOFF"][n - 1], "slot": slot})
    pkg = SeriesPackage.model_validate({
        "slug": SLUG, "name": "Ballot Math (demo)", "status": "paused",
        "bible": "## Audience\n\nGeneral audience, no maths background.\n\n## Length\n\n60–75 s.\n",
        "plan": plan,
        "skills": {"script": "Malayalam, one running example.", "visuals": "Green and black, Byte reacts.",
                   "rubric": "- Hook lands in the first 3 seconds\n- One running example throughout\n- Intuition before formula\n"
                             "- Fits the slot length\n- Ends on the keyword CTA\n- No cliffhanger",
                   "auto-case-study": "Write the case study."},
        "automations": [{"name": "case-study", "order": 1}],
    })
    save_package(session, uid, pkg, mode="create", source=VersionSource.USER)
    series = get_series(session, uid, SLUG)
    eps = {e.number: e for e in series.episodes}
    runner = fake_runner()

    with tempfile.TemporaryDirectory() as tmp:
        take = Path(tmp) / "take.wav"
        _tone(take)
        # Ep 4: all the way to live, with stats
        e4 = eps[4]
        pipeline.advance(session, e4, runner, scorer=DurationScorer())
        pipeline.approve_script(session, e4)
        pipeline.attach_recording(session, e4, take)
        pipeline.advance(session, e4, runner, scorer=DurationScorer())
        pipeline.approve_audio(session, e4)
        pipeline.advance(session, e4, runner, scorer=DurationScorer())
        pipeline.approve_video(session, e4)
        pipeline.advance(session, e4, runner, scorer=DurationScorer())
        pipeline.mark_published(session, e4, {"manual": True})
        session.add(InsightSnapshot(episode_id=e4.id, external_id="demo", metrics={
            "views": 18420, "reach": 15110, "likes": 912, "comments": 340, "shares": 128, "saved": 205,
            "ig_reels_avg_watch_time": 21400}))
        # Ep 1: waiting for video approval
        e1 = eps[1]
        pipeline.advance(session, e1, runner, scorer=DurationScorer())
        pipeline.approve_script(session, e1)
        pipeline.attach_recording(session, e1, take)
        pipeline.advance(session, e1, runner, scorer=DurationScorer())
        pipeline.approve_audio(session, e1)
        pipeline.advance(session, e1, runner, scorer=DurationScorer())
        # Ep 2: script ready; Ep 3: waiting for recording
        pipeline.advance(session, eps[2], runner)
        pipeline.advance(session, eps[3], runner)
        pipeline.approve_script(session, eps[3])
    _make_proposal(session, series, "learnings",
                   "- Open with a question the viewer can answer in their head\n- Name the running example in the first 5 seconds\n",
                   "Across 3 reels, question hooks held viewers about 4 s longer on average.", VersionSource.ANALYSIS)
    series.status = SeriesStatus.PAUSED
    session.flush()
    return {"slug": SLUG, "episodes": {n: e.state.value for n, e in sorted(eps.items())}}
