import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "local: needs the Mac (real Claude, Redis, torch, Meta); run with REELD_LOCAL_TESTS=1")
    config.addinivalue_line("markers", "ffmpeg: needs ffmpeg and rsvg-convert/cairosvg")


def pytest_collection_modifyitems(config, items):
    import shutil

    run_local = os.environ.get("REELD_LOCAL_TESTS") == "1"
    for item in items:
        if "local" in item.keywords and not run_local:
            item.add_marker(pytest.mark.skip(reason="local test: set REELD_LOCAL_TESTS=1"))
        if "ffmpeg" in item.keywords and not shutil.which("ffmpeg"):
            item.add_marker(pytest.mark.skip(reason="ffmpeg not installed"))


@pytest.fixture(autouse=True)
def _media_root(tmp_path, monkeypatch):
    from suite import media

    monkeypatch.setattr(media, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setenv("REELD_EAGER", "1")
    from suite import runner as runner_mod

    yield
    runner_mod.set_runner(None)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fresh SQLite database migrated with Alembic, plus one user."""
    from alembic import command
    from alembic.config import Config

    from suite import db as suite_db
    from suite.users import create_user

    url = f"sqlite:///{tmp_path / 'suite.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("REELD_USER_EMAIL", raising=False)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    suite_db.configure(url)
    with suite_db.session_scope() as s:
        create_user(s, "ajwad@example.com", "Ajwad")
    yield suite_db
    suite_db._engine = None


def sample_package(**overrides):
    from suite.package import SeriesPackage

    data = {
        "slug": "ballot-math",
        "name": "Ballot Math",
        "bible": (
            "Explains voting systems with maths.\n\n"
            "## Audience\n\nGeneral Malayali audience, no maths background.\n\n"
            "## Length\n\n60–75 s voiceover.\n"
        ),
        "plan": [
            {
                "number": 1,
                "title": "Why FPTP makes two parties",
                "core_idea": "Duverger's law via spoiler votes",
                "hook": "US midterms: only two names matter",
                "keyword": "FPTP",
                "slot": {"kind": "precise", "date": "2026-12-01", "time": "19:30"},
            },
            {
                "number": 2,
                "title": "Dynasties under FPTP",
                "core_idea": "Safe seats reward name recognition",
                "hook": "Same surname, third generation",
                "keyword": "DYNASTY",
                "slot": {"kind": "week", "date": "2026-12-07"},
            },
            {
                "number": 3,
                "title": "Caste maths",
                "core_idea": "Pluralities let blocs win",
                "hook": "28% can be a landslide",
                "keyword": "BLOC",
                "slot": {"kind": "after", "after_episode": 2},
                "overrides": {"Length": "90 s"},
            },
        ],
        "skills": {
            "script": "Write in Malayalam. One running example.",
            "rubric": "\n".join(
                [
                    "- Hook lands in the first 3 seconds",
                    "- One running example throughout",
                    "- Intuition before any formula",
                    "- Fits the slot length",
                    "- Ends on the comment-keyword CTA",
                    "- No cliffhanger",
                ]
            ),
            "visuals": "Green and black, Byte mascot reacts.",
            "auto-blog": "Write the case study as markdown for armedjuror.github.io.",
        },
        "automations": [{"name": "blog", "order": 1}],
    }
    data.update(overrides)
    return SeriesPackage.model_validate(data)


@pytest.fixture()
def take(tmp_path):
    """6 s of tone bursts (1.1 s on, 0.5 s off): four utterances."""
    import subprocess

    path = tmp_path / "take.wav"
    expr = "if(lt(mod(t\\,1.6)\\,1.1)\\,0.4*sin(2*PI*220*t)\\,0)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s=44100:d=6", str(path)], check=True)
    return path


SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920">'
       b'<rect width="1080" height="1920" fill="#1E6B47"/></svg>')


def scripted_handler(overrides=None):
    """Fake Claude: answers each built-in prompt with a plausible output."""
    import json

    from suite.runner import StepResult

    overrides = overrides or {}
    state = {"critic": 0}

    def handler(req):
        p = req.prompt
        for key, fn in overrides.items():
            if key in p:
                out = fn(req)
                if out is not None:
                    return out
        if "strict reviewer" in p:
            state["critic"] += 1
            ok = state["critic"] >= 2
            return json.dumps({"pass": ok, "summary": "s", "results": [
                {"criterion": "Hook lands in the first 3 seconds", "pass": ok, "evidence": "", "fix": "sharper"}]})
        if "research note" in p:
            return "Research.\nSources: https://example.com"
        if "Revise" in p:
            return "Line one revised\nLine two\nLine three\nLine four"
        if "script following" in p:
            return "Line one\nLine two\nLine three\nLine four"
        if "Design the visuals" in p:
            return StepResult(output=json.dumps([{"start": 0, "file": "scenes/scene-01.svg"},
                                                 {"start": 2.0, "file": "scenes/scene-02.svg"}]),
                              session_id="vis", raw={}, files={"scenes/scene-01.svg": SVG, "scenes/scene-02.svg": SVG})
        if "Run this automation" in p:
            return "# Case study\n\nBody."
        if "post copy" in p:
            return json.dumps({"caption": "A reel", "hashtags": ["math"], "dm_message": "Here it is"})
        if "continuity entry" in p:
            return "## Ep 1: Why FPTP makes two parties\n- Covered: spoilers"
        if "Publish this reel" in p:
            return json.dumps({"scheduled": True, "post_id": "p1", "automation_id": "a1", "scheduled_for": None})
        if "analyst" in p:
            return json.dumps({"digest": "Question hooks win.", "proposals": [
                {"target": "learnings", "new_content": "- Open with a question", "rationale": "3 of 3 reels"}]})
        if "pairs of scripts" in p:
            return json.dumps({"learnings": "- Shorter lines", "rationale": "edits shortened lines"})
        raise AssertionError(f"unexpected prompt: {p[:80]}")

    return handler


class ToneScorer:
    """Scores the `take` fixture as: utterance 1 = fumbled line 0, 2 = line 0 retake, 3 = line 1, 4 = line 2."""

    plan = {0: None, 1: 0, 2: 1, 3: 2}

    def __init__(self, lines):
        self.lines = lines

    def score(self, audio, span, text):
        if span.end - span.start > 1.5:
            return 0.1
        idx = int((span.start + 0.2) // 1.6)
        want = self.plan.get(idx)
        if idx == 0:
            return 0.6 if text == self.lines[0] else 0.1  # a take of line 0, replaced later
        return 0.9 if want is not None and text == self.lines[want] else 0.1
