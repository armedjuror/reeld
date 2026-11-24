"""Claude Code runner (spec §2, §9, §10).

Every LLM step runs as `claude -p` in a throwaway directory holding only the
materialised context. The step's answer is read from `output.md` (or
`output.json`) that Claude writes there, falling back to the final message.

Usage limits are detected from the JSON result and raised as UsageLimitError
with the reset time and the session ID, so the pipeline can pause the step
and resume it with `claude --resume` after the reset.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

DEFAULT_TIMEOUT_S = int(os.environ.get("REELD_CLAUDE_TIMEOUT", "900"))
LIMIT_FALLBACK = timedelta(minutes=int(os.environ.get("REELD_LIMIT_BACKOFF_MIN", "30")))
LOCAL_TZ = os.environ.get("REELD_TZ", "Asia/Kolkata")

OUTPUT_INSTRUCTIONS = """\
Working rules for this step:
- Read context.md (and SKILL.md if present) in the current directory first.
- Put your final deliverable in {output_file} in the current directory, and
  nothing else in that file. Reply with one short line when done.
- Do not ask questions; make the most reasonable choice and note it in the file.
"""


class RunnerError(RuntimeError):
    """A step failed for a reason other than usage limits."""

    def __init__(self, message: str, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


class UsageLimitError(RuntimeError):
    def __init__(self, message: str, resume_at: datetime, session_id: str | None):
        super().__init__(message)
        self.resume_at = resume_at
        self.session_id = session_id


@dataclass
class StepRequest:
    prompt: str
    files: dict[str, str] = field(default_factory=dict)  # name -> content to place in workdir
    output_file: str = "output.md"
    resume_session_id: Optional[str] = None
    mcp_config: Optional[dict[str, Any]] = None
    allowed_tools: Optional[list[str]] = None
    model: Optional[str] = None
    keep_files: tuple[str, ...] = ()  # extra produced files to return (e.g. scene svgs)


@dataclass
class StepResult:
    output: str
    session_id: Optional[str]
    raw: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)
    cost_usd: float = 0.0


class Runner(Protocol):
    def run(self, req: StepRequest) -> StepResult: ...


# ─── Usage-limit parsing ──────────────────────────────────────────────────────

_LIMIT_HINT = re.compile(
    r"(usage limit|rate limit|limit reached|hit your limit|limit will reset|resets? (?:at )?\d)",
    re.IGNORECASE,
)
_EPOCH = re.compile(r"limit reached\|(\d{9,11})", re.IGNORECASE)
_RESET_CLOCK = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)


def is_usage_limit(text: str) -> bool:
    return bool(text and _LIMIT_HINT.search(text))


def parse_reset_time(text: str, now: datetime | None = None) -> datetime:
    """Best-effort reset time from a limit message; falls back to now + backoff."""
    now = now or datetime.now(timezone.utc)
    m = _EPOCH.search(text or "")
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    m = _RESET_CLOCK.search(text or "")
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        try:
            tz = ZoneInfo((m.group(4) or LOCAL_TZ).strip())
        except Exception:
            tz = ZoneInfo(LOCAL_TZ)
        if 0 <= hour < 24 and 0 <= minute < 60:
            local_now = now.astimezone(tz)
            candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= local_now:
                candidate += timedelta(days=1)
            return candidate.astimezone(timezone.utc) + timedelta(minutes=1)
    return now + LIMIT_FALLBACK


# ─── Real runner ──────────────────────────────────────────────────────────────


class ClaudeCLIRunner:
    """Runs `claude -p` with JSON output in a temporary directory."""

    def __init__(
        self,
        binary: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        model: str | None = None,
        extra_args: list[str] | None = None,
        workroot: str | None = None,
    ):
        self.binary = binary or os.environ.get("REELD_CLAUDE_BIN", "claude")
        self.timeout_s = timeout_s
        self.model = model or os.environ.get("REELD_CLAUDE_MODEL") or None
        self.extra_args = extra_args or []
        self.workroot = workroot

    def build_command(self, req: StepRequest, mcp_path: Path | None) -> list[str]:
        cmd = [self.binary, "-p", req.prompt, "--output-format", "json"]
        if req.resume_session_id:
            cmd += ["--resume", req.resume_session_id]
        model = req.model or self.model
        if model:
            cmd += ["--model", model]
        if mcp_path is not None:
            cmd += ["--mcp-config", str(mcp_path), "--strict-mcp-config"]
        # Steps are unattended: allow file edits in the throwaway workdir and
        # whatever MCP tools the step was configured with.
        tools = req.allowed_tools or ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
        if req.mcp_config:
            tools = tools + [f"mcp__{name}" for name in req.mcp_config.get("mcpServers", {})]
        cmd += ["--allowedTools", *tools, "--permission-mode", "acceptEdits"]
        return cmd + self.extra_args

    def run(self, req: StepRequest) -> StepResult:
        workdir = Path(tempfile.mkdtemp(prefix="reeld-step-", dir=self.workroot))
        try:
            for name, content in req.files.items():
                path = workdir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            mcp_path = None
            if req.mcp_config:
                mcp_path = workdir / ".step-mcp.json"
                mcp_path.write_text(json.dumps(req.mcp_config), encoding="utf-8")
            full = StepRequest(**{**req.__dict__})
            if not req.resume_session_id:
                full.prompt = OUTPUT_INSTRUCTIONS.format(output_file=req.output_file) + "\n" + req.prompt
            try:
                proc = subprocess.run(
                    self.build_command(full, mcp_path),
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError(f"claude -p timed out after {self.timeout_s}s") from exc
            return self._parse(proc, workdir, req)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _parse(proc: subprocess.CompletedProcess, workdir: Path, req: StepRequest) -> StepResult:
        raw: dict[str, Any] = {}
        try:
            raw = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            raw = {}
        text = str(raw.get("result") or "")
        session_id = raw.get("session_id")
        combined = " ".join([text, proc.stderr or "", proc.stdout if not raw else ""])
        failed = proc.returncode != 0 or raw.get("is_error") or not raw
        if failed:
            if is_usage_limit(combined):
                raise UsageLimitError(combined.strip()[:500], parse_reset_time(combined), session_id)
            raise RunnerError(
                f"claude -p failed (exit {proc.returncode}): {combined.strip()[:800]}", session_id
            )
        out_path = workdir / req.output_file
        output = out_path.read_text(encoding="utf-8") if out_path.exists() else text
        files = {}
        for pattern in req.keep_files:
            for p in sorted(workdir.glob(pattern)):
                if p.is_file():
                    files[str(p.relative_to(workdir))] = p.read_bytes()
        return StepResult(
            output=output,
            session_id=session_id,
            raw=raw,
            files=files,
            cost_usd=float(raw.get("total_cost_usd") or 0.0),
        )


# ─── Fake runner (tests and dry runs) ─────────────────────────────────────────


class FakeRunner:
    """Scripted runner: a handler maps each request to an output (or raises)."""

    def __init__(self, handler: Callable[[StepRequest], str | StepResult]):
        self.handler = handler
        self.calls: list[StepRequest] = []

    def run(self, req: StepRequest) -> StepResult:
        self.calls.append(req)
        out = self.handler(req)
        if isinstance(out, StepResult):
            return out
        return StepResult(output=out, session_id=f"fake-{len(self.calls)}", raw={})


_default_runner: Runner | None = None


def get_runner() -> Runner:
    global _default_runner
    if _default_runner is None:
        _default_runner = ClaudeCLIRunner()
    return _default_runner


def set_runner(runner: Runner | None) -> None:
    global _default_runner
    _default_runner = runner


def extract_json(text: str) -> Any:
    """Parse JSON from a model output that may wrap it in prose or fences."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model output")
