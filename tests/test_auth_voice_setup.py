import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import sample_package
from suite import audio, voice
from suite.package import save_package
from suite.users import current_user_id


@pytest.fixture()
def app_client(db, monkeypatch):
    monkeypatch.setattr("suite.worker.kick", lambda episode_id: False)
    monkeypatch.setattr("suite.worker.broker_ok", lambda: False)
    from suite.app import app

    return TestClient(app)


# ─── Sign-in ──────────────────────────────────────────────────────────────────


def test_open_mode_without_key(app_client, monkeypatch):
    monkeypatch.delenv("REELD_AUTH_KEY", raising=False)
    me = app_client.get("/api/suite/me").json()
    assert me["signed_in"] and not me["login_required"] and me["user"]["email"] == "ajwad@example.com"
    assert app_client.get("/api/suite/home").status_code == 200


def test_key_login_flow(app_client, monkeypatch):
    monkeypatch.setenv("REELD_AUTH_KEY", "s3cret-key")
    monkeypatch.setattr("suite.auth.time.sleep", lambda s: None)
    assert app_client.get("/api/suite/home").status_code == 401
    assert app_client.get("/api/suite/me").json() == {"signed_in": False, "login_required": True, "user": None}
    assert app_client.post("/api/suite/auth/login", json={"key": "wrong"}).status_code == 401
    assert app_client.post("/api/suite/auth/login", json={"key": "s3cret-key"}).json()["ok"]
    assert app_client.get("/api/suite/home").status_code == 200
    assert app_client.get("/api/suite/me").json()["signed_in"]
    app_client.post("/api/suite/auth/logout")
    assert app_client.get("/api/suite/home").status_code == 401
    assert "reeld" in app_client.get("/studio").text  # the page itself loads, then asks for the key


# ─── Claude & MCP setup ───────────────────────────────────────────────────────


def test_claude_setup_and_skill_zip(app_client, monkeypatch):
    monkeypatch.delenv("REELD_AUTH_KEY", raising=False)
    d = app_client.get("/api/suite/claude-setup").json()
    cmd = d["mcp"]["claude_code"]
    # the server name must come before the variadic -e flags
    assert cmd.startswith("claude mcp add --scope user reeld -e ") and cmd.endswith("-m suite.mcp_server")
    assert "REELD_USER_EMAIL=ajwad@example.com" in cmd and "PYTHONPATH=" in cmd
    env = d["mcp"]["desktop_json"]["mcpServers"]["reeld"]["env"]
    assert env["REELD_USER_EMAIL"] == "ajwad@example.com"
    names = {s["name"] for s in d["skills"]}
    assert {"reeld-series-package", "reeld-voice-profile"} <= names
    assert d["voice"]["total"] == len(voice.SECTIONS)
    z = app_client.get("/api/suite/skills/reeld-voice-profile.zip")
    assert z.status_code == 200
    files = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert files == ["reeld-voice-profile/SKILL.md"]
    assert app_client.get("/api/suite/skills/..%2Fsuite.zip").status_code == 404


def test_status_without_claude(app_client, monkeypatch):
    monkeypatch.setenv("REELD_CLAUDE_BIN", "definitely-not-claude")
    st = app_client.get("/api/suite/claude-setup/status").json()
    assert st["claude"]["installed"] is False


# ─── Voice profile ────────────────────────────────────────────────────────────


def test_voice_coverage_and_section_update():
    md = voice.update_section("", "Tone", "Casual 4/5. " + "word " * 20)
    md = voice.update_section(md, "Audience", "Kerala students")
    cov = voice.coverage(md)
    status = {s["section"]: s["status"] for s in cov["sections"]}
    assert status["Tone"] == "ok" and status["Audience"] == "thin" and status["Hooks"] == "missing"
    md2 = voice.update_section(md, "tone", "Calm")  # same section, case-insensitive
    assert md2.count("## Tone") == 1 and "Calm" in md2 and "Kerala students" in md2
    assert "<!--" in voice.template()


def _call(name, args):
    from suite.mcp_server import server

    result = asyncio.run(server.call_tool(name, args))
    payload = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    return payload.get("result", payload) if isinstance(payload, dict) and set(payload) == {"result"} else payload


def test_mcp_voice_tools_and_prompt(db):
    assert _call("check_voice_profile", {})["ok"] == 0
    tpl = _call("get_voice_template", {})
    assert len(tpl["sections"]) == len(voice.SECTIONS) and tpl["sections"][2]["heading"] == "Language"
    long = "Malayalam with English technical words; script written in Malayalam; captions in English " * 2
    r = _call("update_voice_section", {"heading": "Language", "content": long})
    assert r["ok"] and r["progress"] == f"1/{len(voice.SECTIONS)} sections clear"
    _call("update_voice_section", {"heading": "Tone", "content": "Casual"})
    prof = _call("get_user_profile", {})["content"]
    assert "## Language" in prof and "## Tone" in prof
    assert _call("update_voice_section", {"heading": " ", "content": "x"})["ok"] is False

    from suite.mcp_server import server

    prompts = asyncio.run(server.list_prompts())
    assert [p.name for p in prompts] == ["voice_interview"]
    got = asyncio.run(server.get_prompt("voice_interview", {}))
    text = got.messages[0].content.text
    assert "check_voice_profile" in text and "update_voice_section" in text and not text.lstrip().startswith("---")


# ─── Regression: speech model that matches nothing ────────────────────────────


class _NoMatch:
    def score(self, audio_path, span, text):
        return 0.0


@pytest.mark.ffmpeg
def test_alignment_with_no_match_falls_back(take, tmp_path):
    res = audio.process_take(take, "Line one\nLine two", tmp_path / "out.m4a", scorer=_NoMatch())
    assert res["method"] == "silence-only" and "matched no line" in res["note"]
    assert len(res["kept_spans"]) == res["utterances"]


@pytest.mark.ffmpeg
def test_demo_seeds_even_with_a_speech_model(db, monkeypatch):
    # On a Mac with torch installed, default_scorer() is MMS; the demo's tone
    # bursts match no text. The demo must still reach every gate.
    monkeypatch.setattr(audio, "default_scorer", lambda: _NoMatch())
    from suite.demo import seed

    with db.session_scope() as s:
        out = seed(s)
    assert out["episodes"][1] == "video_review" and out["episodes"][4] == "live"


class _Broken:
    def score(self, audio_path, span, text):
        raise ImportError("TorchCodec is required for load_with_torchcodec")


@pytest.mark.ffmpeg
def test_model_error_falls_back_instead_of_stalling(take, tmp_path):
    # torchaudio>=2.9 without torchcodec raised here and left episodes stuck in audio_processing
    res = audio.process_take(take, "Line one\nLine two", tmp_path / "out.m4a", scorer=_Broken())
    assert res["method"] == "silence-only" and "Speech model error (ImportError" in res["note"]


@pytest.mark.ffmpeg
def test_mms_decodes_with_ffmpeg_not_torchaudio(take):
    np = pytest.importorskip("numpy")
    from types import SimpleNamespace

    class T:  # minimal stand-in for a torch tensor
        def __init__(self, a):
            self.a = a

        def unsqueeze(self, dim):
            return T(np.expand_dims(self.a, dim))

    scorer = audio.MMSScorer.__new__(audio.MMSScorer)
    scorer.torch = SimpleNamespace(from_numpy=T)
    scorer.bundle = SimpleNamespace(sample_rate=16000)
    scorer._cache = {}
    wav, sr = scorer._wave(take)
    assert sr == 16000 and wav.a.shape[0] == 1
    assert abs(wav.a.shape[1] / 16000 - 6.0) < 0.05  # the 6 s fixture, resampled to 16 kHz mono
    assert wav.a.dtype == np.float32 and 0.3 < float(abs(wav.a).max()) < 0.5
