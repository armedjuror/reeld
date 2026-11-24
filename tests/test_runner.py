import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from suite.runner import (
    ClaudeCLIRunner,
    RunnerError,
    StepRequest,
    UsageLimitError,
    extract_json,
    is_usage_limit,
    parse_reset_time,
)

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


@pytest.mark.parametrize("text", [
    "Claude AI usage limit reached|1764000000",
    "You've hit your limit · resets 4pm (Asia/Kolkata)",
    "5-hour limit reached ∙ resets 3am",
    "Usage limit reached. Your limit will reset at 7:30 pm.",
])
def test_detects_usage_limits(text):
    assert is_usage_limit(text)


def test_ordinary_errors_are_not_limits():
    assert not is_usage_limit("Error: file not found")
    assert not is_usage_limit("")


def test_reset_time_formats():
    assert parse_reset_time("Claude AI usage limit reached|1764000000", NOW) == datetime.fromtimestamp(1764000000, tz=timezone.utc)
    t = parse_reset_time("hit your limit · resets 4pm (Asia/Kolkata)", NOW).astimezone(ZoneInfo("Asia/Kolkata"))
    assert (t.hour, t.minute, t.date().day) == (16, 1, 24)
    t = parse_reset_time("limit reached ∙ resets 3am", NOW).astimezone(ZoneInfo("Asia/Kolkata"))
    assert (t.hour, t.date().day) == (3, 25)  # 3am already passed today → tomorrow
    t = parse_reset_time("limit will reset at 7:30 pm", NOW).astimezone(ZoneInfo("Asia/Kolkata"))
    assert (t.hour, t.minute) == (19, 31)
    fallback = parse_reset_time("usage limit reached", NOW)
    assert 25 <= (fallback - NOW).total_seconds() / 60 <= 35


def _proc(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["claude"], code, stdout=stdout, stderr=stderr)


def test_parse_success_reads_output_file(tmp_path):
    (tmp_path / "output.md").write_text("the deliverable")
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "scene-01.svg").write_text("<svg/>")
    raw = {"type": "result", "is_error": False, "result": "done", "session_id": "s1", "total_cost_usd": 0.02}
    res = ClaudeCLIRunner._parse(_proc(json.dumps(raw)), tmp_path,
                                 StepRequest(prompt="x", keep_files=("scenes/*.svg",)))
    assert res.output == "the deliverable" and res.session_id == "s1" and res.cost_usd == 0.02
    assert list(res.files) == ["scenes/scene-01.svg"]


def test_parse_falls_back_to_final_message(tmp_path):
    raw = {"is_error": False, "result": "inline answer", "session_id": "s1"}
    assert ClaudeCLIRunner._parse(_proc(json.dumps(raw)), tmp_path, StepRequest(prompt="x")).output == "inline answer"


def test_parse_limit_and_failure(tmp_path):
    raw = {"is_error": True, "result": "You've hit your limit · resets 4pm", "session_id": "s9"}
    with pytest.raises(UsageLimitError) as exc:
        ClaudeCLIRunner._parse(_proc(json.dumps(raw), code=1), tmp_path, StepRequest(prompt="x"))
    assert exc.value.session_id == "s9"
    with pytest.raises(RunnerError):
        ClaudeCLIRunner._parse(_proc("", "boom", code=1), tmp_path, StepRequest(prompt="x"))


def test_command_flags():
    r = ClaudeCLIRunner(binary="claude", model="opus")
    cmd = r.build_command(StepRequest(prompt="p", resume_session_id="abc",
                                      mcp_config={"mcpServers": {"reeld": {}}}), Path("/tmp/m.json"))
    assert cmd[:4] == ["claude", "-p", "p", "--output-format"]
    assert ["--resume", "abc"] == cmd[cmd.index("--resume"):cmd.index("--resume") + 2]
    assert "--strict-mcp-config" in cmd and "mcp__reeld" in cmd and "--model" in cmd


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('noise [1, 2] noise') == [1, 2]
    with pytest.raises(ValueError):
        extract_json("no json here")
