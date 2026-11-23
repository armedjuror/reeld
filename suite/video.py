"""Render the final reel from Claude's scene SVGs, the clean voice and BGM.

Each scene is a 1080×1920 SVG shown from its start time until the next
scene. SVGs are rasterised with rsvg-convert (or cairosvg), stitched with
the ffmpeg concat demuxer, and mixed with the voice (full) and BGM (faint,
looped, faded out) into an H.264/AAC MP4.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .audio import probe_duration, run_ffmpeg

WIDTH, HEIGHT, FPS = 1080, 1920, 30
BGM_VOLUME = 0.07  # "kept faint"


@dataclass
class Scene:
    start: float
    svg: bytes
    description: str = ""


def rasterise(svg: bytes, out_png: Path) -> None:
    src = out_png.with_suffix(".svg")
    src.write_bytes(svg)
    if shutil.which("rsvg-convert"):
        proc = subprocess.run(
            ["rsvg-convert", "-w", str(WIDTH), "-h", str(HEIGHT), "-b", "black", "-o", str(out_png), str(src)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"rsvg-convert failed: {proc.stderr[:500]}")
        return
    try:
        import cairosvg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install librsvg (brew install librsvg) or `pip install cairosvg`") from exc
    cairosvg.svg2png(bytestring=svg, write_to=str(out_png), output_width=WIDTH, output_height=HEIGHT,
                     background_color="black")


def render_reel(scenes: Sequence[Scene], voice: Path, out: Path, bgm: Optional[Path] = None) -> float:
    """Render and return the duration of the output video."""
    if not scenes:
        raise ValueError("no scenes to render")
    total = probe_duration(voice)
    ordered = sorted(scenes, key=lambda s: s.start)
    ordered[0] = Scene(0.0, ordered[0].svg, ordered[0].description)  # first frame at 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        lines = []
        for k, sc in enumerate(ordered):
            png = tmpd / f"scene-{k:03d}.png"
            rasterise(sc.svg, png)
            end = ordered[k + 1].start if k + 1 < len(ordered) else total
            dur = max(0.04, min(end, total) - sc.start)
            lines += [f"file '{png}'", f"duration {dur:.3f}"]
        lines.append(f"file '{tmpd / f'scene-{len(ordered) - 1:03d}.png'}'")  # concat quirk
        listing = tmpd / "scenes.txt"
        listing.write_text("\n".join(lines) + "\n")

        inputs = ["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(voice)]
        if bgm is not None:
            inputs += ["-stream_loop", "-1", "-i", str(bgm)]
            fade_start = max(0.0, total - 1.5)
            audio_graph = (
                f"[2:a]volume={BGM_VOLUME},afade=t=out:st={fade_start:.2f}:d=1.5[bg];"
                f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            amap = ["-filter_complex", audio_graph, "-map", "0:v", "-map", "[aout]"]
        else:
            amap = ["-map", "0:v", "-map", "1:a"]
        run_ffmpeg([
            *inputs, *amap,
            "-vf", f"fps={FPS},format=yuv420p,scale={WIDTH}:{HEIGHT}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart",
            str(out),
        ])
    return probe_duration(out)
