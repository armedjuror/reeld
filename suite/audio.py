"""Audio stage (spec §5 step 5): pick takes, cut silences, normalise, time lines.

Recording style it expects: read the script line by line, pause briefly
between lines, and when you slip, pause and read the same line again. The
last clean take of each line wins.

Pipeline:
1. `detect_speech` splits the take into utterances on silences (ffmpeg).
2. `select_takes` assigns utterances to script lines in order, keeping the
   last take of each line. Scoring uses a `Scorer`: MMS forced alignment
   when torch/torchaudio are installed (`MMSScorer`, Malayalam via uroman),
   otherwise a duration heuristic that only removes silences.
3. `render_clean` concatenates the kept spans with short gaps and applies
   EBU R128 loudness normalisation.
4. `line_timings` gives each script line a start/end in the clean audio
   (forced alignment when available, else proportional to text length).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

SILENCE_DB = -38  # dBFS threshold
MIN_SILENCE_S = 0.45  # pause that separates utterances
PAD_S = 0.12  # breath kept around each utterance
GAP_S = 0.18  # silence inserted between kept utterances
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"  # reels-friendly loudness
MATCH_THRESHOLD = 0.35  # minimum normalised score for an utterance to count


@dataclass
class Span:
    start: float
    end: float

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Choice:
    line: int  # index into script lines
    spans: list[Span]
    score: float


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")
    return proc


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def detect_speech(path: str | Path, noise_db: int = SILENCE_DB, min_silence: float = MIN_SILENCE_S) -> list[Span]:
    """Non-silent spans (utterances), padded slightly."""
    total = probe_duration(path)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    if len(ends) < len(starts):
        ends.append(total)
    spans, pos = [], 0.0
    for s, e in zip(starts, ends):
        s = max(0.0, s)
        if s - pos > 0.08:
            spans.append(Span(max(0.0, pos - PAD_S), min(total, s + PAD_S)))
        pos = e
    if total - pos > 0.08:
        spans.append(Span(max(0.0, pos - PAD_S), total))
    return spans


def split_lines(script: str) -> list[str]:
    """Script lines to align: non-empty lines, stripped of markdown and stage notes."""
    lines = []
    for raw in (script or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("[") and line.endswith("]")):
            continue
        line = re.sub(r"^[-*>\d.)\s]+", "", line)
        line = re.sub(r"[*_`]", "", line).strip()
        if line:
            lines.append(line)
    return lines


# ─── Scoring and take selection ───────────────────────────────────────────────


class Scorer(Protocol):
    """Score in [0, 1]: how well the audio span matches the text."""

    def score(self, audio: Path, span: Span, text: str) -> float: ...


class DurationScorer:
    """Fallback without a speech model: plausibility from speaking rate.

    It cannot tell a retake from the next line, so with it every utterance is
    kept in order (silence removal only). Used when torchaudio is missing.
    """

    chars_per_sec = 14.0

    def score(self, audio: Path, span: Span, text: str) -> float:
        expected = max(0.6, len(text) / self.chars_per_sec)
        ratio = span.dur / expected
        return max(0.0, 1.0 - abs(1.0 - ratio)) if ratio < 3 else 0.0


def select_takes(
    lines: Sequence[str],
    utterances: Sequence[Span],
    scorer: Scorer,
    audio: Path,
    threshold: float = MATCH_THRESHOLD,
    max_merge: int = 3,
    lookahead: int = 2,
) -> list[Choice]:
    """Walk utterances in order and assign each run of 1..max_merge to a line.

    A run is matched against the current line, the next `lookahead` lines,
    and the previous line (a retake). Matching the same line again replaces
    the earlier take. Unmatched utterances (fillers, noise) are dropped.
    """
    chosen: dict[int, Choice] = {}
    cur = 0  # next line expected
    i = 0
    while i < len(utterances) and lines:
        best: Optional[tuple[float, int, int]] = None  # (score, line, n_utts)
        candidates = range(max(0, cur - 1), min(len(lines), cur + lookahead + 1))
        for n in range(1, max_merge + 1):
            if i + n > len(utterances):
                break
            span = Span(utterances[i].start, utterances[i + n - 1].end)
            for li in candidates:
                sc = scorer.score(audio, span, lines[li])
                # prefer progress: tiny bonus for the expected line
                adj = sc + (0.02 if li == cur else 0.0) - (0.01 * (n - 1))
                if best is None or adj > best[0]:
                    best = (adj, li, n)
        if best is None or best[0] < threshold:
            i += 1
            continue
        _, li, n = best
        spans = list(utterances[i : i + n])
        chosen[li] = Choice(line=li, spans=spans, score=round(best[0], 3))  # retake replaces
        cur = max(cur, li + 1)
        i += n
    return [chosen[k] for k in sorted(chosen)]


# ─── Rendering and timing ─────────────────────────────────────────────────────


def render_clean(audio: Path, spans: Sequence[Span], out: Path, gap: float = GAP_S) -> float:
    """Concatenate spans with short gaps, normalise loudness, write mp3/wav/m4a."""
    if not spans:
        raise ValueError("nothing to keep")
    parts, labels = [], []
    for k, sp in enumerate(spans):
        parts.append(f"[0:a]atrim=start={sp.start:.3f}:end={sp.end:.3f},asetpts=PTS-STARTPTS,"
                     f"apad=pad_dur={gap:.3f}[s{k}]")
        labels.append(f"[s{k}]")
    parts.append("".join(labels) + f"concat=n={len(spans)}:v=0:a=1,{LOUDNORM},aresample=48000[out]")
    codec = ["-c:a", "libmp3lame", "-q:a", "2"] if out.suffix == ".mp3" else (
        ["-c:a", "aac", "-b:a", "192k"] if out.suffix == ".m4a" else [])
    run_ffmpeg(["-i", str(audio), "-filter_complex", ";".join(parts), "-map", "[out]", *codec, str(out)])
    return probe_duration(out)


def proportional_timings(lines: Sequence[str], durations: Sequence[float]) -> list[dict]:
    """Line timings in the clean audio when each line's kept duration is known."""
    t, out = 0.0, []
    for text, d in zip(lines, durations):
        out.append({"text": text, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


def build_alignment(lines: Sequence[str], choices: Sequence[Choice], gap: float = GAP_S) -> list[dict]:
    """Timings of each kept line in the clean audio, from the chosen spans."""
    timings, t = [], 0.0
    for ch in choices:
        start = t
        for sp in ch.spans:
            t += sp.dur + gap
        timings.append({"line": ch.line, "text": lines[ch.line], "start": round(start, 3),
                        "end": round(t - gap, 3), "score": ch.score})
    return timings


def process_take(raw: Path, script: str, out: Path, scorer: Optional[Scorer] = None) -> dict:
    """Full audio stage for one recording. Returns alignment + stats."""
    lines = split_lines(script)
    utterances = detect_speech(raw)
    if not utterances:
        raise ValueError("no speech detected in the recording")
    scorer = scorer or default_scorer()
    method = "forced-alignment" if not isinstance(scorer, DurationScorer) else "silence-only"
    if isinstance(scorer, DurationScorer) or not lines:
        # Without a speech model we can't tell retakes apart: keep everything.
        choices = [Choice(line=min(k, max(0, len(lines) - 1)), spans=[sp], score=0.0)
                   for k, sp in enumerate(utterances)]
        kept = [sp for ch in choices for sp in ch.spans]
        duration = render_clean(raw, kept, out)
        total_chars = sum(len(l) for l in lines) or 1
        timings = proportional_timings(lines, [duration * len(l) / total_chars for l in lines]) if lines else []
        missing: list[int] = []
    else:
        choices = select_takes(lines, utterances, scorer, raw)
        kept = [sp for ch in choices for sp in ch.spans]
        if not kept:
            raise ValueError("no utterance matched the script")
        duration = render_clean(raw, kept, out)
        timings = build_alignment(lines, choices)
        got = {c.line for c in choices}
        missing = [k for k in range(len(lines)) if k not in got]
    return {
        "method": method,
        "raw_duration": round(probe_duration(raw), 2),
        "clean_duration": round(duration, 2),
        "utterances": len(utterances),
        "kept_spans": [[round(s.start, 3), round(s.end, 3)] for s in kept],
        "lines": timings,
        "missing_lines": missing,
    }


def apply_extra_cuts(clean: Path, cuts: Sequence[tuple[float, float]], out: Path) -> float:
    """Remove user-marked ranges (seconds in the clean audio) and re-render."""
    total = probe_duration(clean)
    keep, pos = [], 0.0
    for s, e in sorted(cuts):
        if s > pos:
            keep.append(Span(pos, s))
        pos = max(pos, e)
    if pos < total:
        keep.append(Span(pos, total))
    return render_clean(clean, keep, out, gap=0.0)


# ─── MMS forced alignment (needs torch + torchaudio + uroman; run on the Mac) ─


class MMSScorer:
    """Scores spans with torchaudio's MMS_FA multilingual aligner.

    Text is romanised with uroman, so Malayalam works. Score = exp(mean
    log-prob of the aligned tokens), which lands in (0, 1].
    """

    def __init__(self, device: str = "cpu"):
        import torch  # noqa: F401
        import torchaudio

        self.torch = torch
        self.torchaudio = torchaudio
        self.bundle = torchaudio.pipelines.MMS_FA
        self.model = self.bundle.get_model(with_star=False).to(device)
        self.dictionary = self.bundle.get_dict(star=None)
        self.device = device
        self._cache: dict[str, tuple] = {}
        try:
            import uroman

            self._uroman = uroman.Uroman()
        except Exception:
            self._uroman = None

    def romanize(self, text: str) -> str:
        if self._uroman is not None:
            text = self._uroman.romanize_string(text)
        text = text.lower().replace("’", "'")
        return re.sub(r"[^a-z' ]", " ", text)

    def _wave(self, audio: Path):
        key = str(audio)
        if key not in self._cache:
            wav, sr = self.torchaudio.load(key)
            wav = wav.mean(0, keepdim=True)
            if sr != self.bundle.sample_rate:
                wav = self.torchaudio.functional.resample(wav, sr, self.bundle.sample_rate)
            self._cache = {key: (wav, self.bundle.sample_rate)}
        return self._cache[key]

    def score(self, audio: Path, span: Span, text: str) -> float:
        torch = self.torch
        wav, sr = self._wave(audio)
        seg = wav[:, int(span.start * sr): int(span.end * sr)]
        tokens = [self.dictionary[c] for word in self.romanize(text).split() for c in word if c in self.dictionary]
        if seg.shape[1] < sr * 0.2 or not tokens:
            return 0.0
        with torch.inference_mode():
            emission, _ = self.model(seg.to(self.device))
        if emission.shape[1] < len(tokens):
            return 0.0
        targets = torch.tensor([tokens], dtype=torch.int32, device=self.device)
        try:
            aligned, scores = self.torchaudio.functional.forced_align(emission, targets, blank=0)
        except Exception:
            return 0.0
        nonblank = scores[0][aligned[0] != 0]
        mean_lp = float(nonblank.mean()) if nonblank.numel() else -10.0
        import math

        return math.exp(mean_lp)


def default_scorer() -> Scorer:
    try:
        return MMSScorer()
    except Exception:
        return DurationScorer()
