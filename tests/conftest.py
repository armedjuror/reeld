import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
