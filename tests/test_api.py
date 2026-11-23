import pytest
from fastapi.testclient import TestClient

from conftest import scripted_handler
from suite import runner as runner_mod
from suite.runner import FakeRunner

pytestmark = pytest.mark.ffmpeg


@pytest.fixture()
def client(db, monkeypatch):
    # the worker isn't running in tests: kicks are no-ops
    monkeypatch.setattr("suite.worker.kick", lambda episode_id: False)
    monkeypatch.setattr("suite.worker.broker_ok", lambda: False)
    from suite.app import app
    from suite.demo import seed

    with db.session_scope() as s:
        seed(s)
    return TestClient(app)


def _ep(client, number):
    series = client.get("/api/suite/series").json()
    return next(e for e in series[0]["episodes"] if e["number"] == number)


def test_ui_and_home(client):
    assert "reeld" in client.get("/").text
    h = client.get("/api/suite/home").json()
    types = sorted(t["type"] for t in h["tasks"])
    assert types == ["approve_script", "approve_video", "record", "review_proposal"]
    assert h["today"] and h["today"][0]["needs"] == ["approve_video"]
    assert client.post("/api/suite/open").json()["queued"] is False


def test_script_gate(client):
    ep = _ep(client, 2)
    d = client.get(f"/api/suite/episodes/{ep['id']}").json()
    assert d["state"] == "script_review" and d["critic"]["results"]
    r = client.post(f"/api/suite/episodes/{ep['id']}/approve-script", json={"content": d["script"] + "\nOne more line"})
    assert r.status_code == 200 and r.json()["state"] == "awaiting_recording"
    d = client.get(f"/api/suite/episodes/{ep['id']}").json()
    assert d["script_versions"][-1]["source"] == "user"
    # approving twice is a conflict, not a crash
    assert client.post(f"/api/suite/episodes/{ep['id']}/approve-script", json={}).status_code == 409


def test_script_feedback_uses_runner(client):
    runner_mod.set_runner(FakeRunner(scripted_handler({"creator's note": lambda r: "Shorter script"})))
    ep = _ep(client, 2)
    r = client.post(f"/api/suite/episodes/{ep['id']}/script-feedback", json={"note": "shorter"})
    assert r.status_code == 200 and r.json()["error"] is None
    assert client.get(f"/api/suite/episodes/{ep['id']}").json()["script"].startswith("Shorter script")


def test_recording_upload_and_media(client, take):
    ep = _ep(client, 3)
    with open(take, "rb") as fh:
        r = client.post(f"/api/suite/episodes/{ep['id']}/recording", files={"file": ("take.wav", fh, "audio/wav")})
    assert r.status_code == 200 and r.json()["state"] == "audio_processing"
    d = client.get(f"/api/suite/episodes/{ep['id']}").json()
    assert d["raw_url"] and client.get(d["raw_url"]).status_code == 200


def test_video_gate_and_manual_publish(client):
    ep = _ep(client, 1)
    d = client.get(f"/api/suite/episodes/{ep['id']}").json()
    assert d["video_url"] and client.get(d["video_url"]).headers["content-type"].startswith("video/")
    assert client.post(f"/api/suite/episodes/{ep['id']}/approve-video").json()["state"] == "automations"
    live = _ep(client, 4)
    assert client.post(f"/api/suite/episodes/{live['id']}/mark-published", json={}).status_code == 409


def test_proposal_flow(client):
    h = client.get("/api/suite/home").json()
    pid = next(t for t in h["tasks"] if t["type"] == "review_proposal")["payload"]["proposal_id"]
    p = client.get(f"/api/suite/proposals/{pid}").json()
    assert p["label"] == "learnings" and any(l.startswith("+") for l in p["diff"])
    assert client.post(f"/api/suite/proposals/{pid}/accept", json={}).json()["version"] >= 1
    assert client.post(f"/api/suite/proposals/{pid}/accept", json={}).status_code == 409


def test_calendar_slot_and_series_status(client):
    ep = _ep(client, 5)
    assert client.post(f"/api/suite/episodes/{ep['id']}/slot", json={"kind": "precise"}).status_code == 400
    r = client.post(f"/api/suite/episodes/{ep['id']}/slot", json={"kind": "precise", "date": "2026-12-20", "time": "18:00"})
    assert r.status_code == 200
    cal = client.get("/api/suite/calendar?start=2026-12-01&days=31").json()
    assert any(i["number"] == 5 and i["start"] == "2026-12-20" for i in cal["items"])
    assert client.post("/api/suite/series/demo-ballot-math/status", json={"status": "active"}).json()["status"] == "active"


def test_settings(client):
    assert client.put("/api/suite/profile", json={"content": "## Tone\n\nWarm"}).json()["changed"]
    assert client.get("/api/suite/profile").json()["content"].startswith("## Tone")
    view = client.put("/api/suite/integrations/meta", json={"data": {"access_token": "IGAAverylongtoken123"}}).json()
    assert view["access_token"] == "IGAA…n123" and "expires_at" in view
    # saving the masked value back doesn't overwrite the token
    client.put("/api/suite/integrations/meta", json={"data": {"access_token": "IGAA…n123"}})
    assert client.get("/api/suite/integrations").json()["meta"]["access_token"] == "IGAA…n123"
    r = client.post("/api/suite/library?kind=bgm&series=demo-ballot-math", files={"file": ("bed.mp3", b"ID3fake", "audio/mpeg")})
    assert r.status_code == 200
    assert client.get("/api/suite/library").json()[0]["series"] == "demo-ballot-math"
