import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FPS = 30
AUDIO_PAD = 0.08  # seconds padded each side to avoid clipping words

# ─── Utilities ────────────────────────────────────────────────────────────────

def run(cmd: list, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{result.stderr}")
    return result

def hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def get_video_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])

def load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    if font_path and os.path.exists(font_path):
        return ImageFont.truetype(font_path, size)
    for fallback in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(fallback):
            return ImageFont.truetype(fallback, size)
    return ImageFont.load_default()

# ─── Caption parsing ──────────────────────────────────────────────────────────

def parse_caption(caption: str) -> list:
    parts = []
    for p in re.split(r'(\*[^*]+\*)', caption):
        if p.startswith('*') and p.endswith('*'):
            parts.append({"text": p[1:-1], "highlight": True})
        elif p:
            parts.append({"text": p, "highlight": False})
    return parts

def get_partial_caption(caption: str, n_visible: int) -> str:
    result = []
    visible = 0
    inside_marker = False
    i = 0
    while i < len(caption):
        ch = caption[i]
        if ch == '*':
            inside_marker = not inside_marker
            result.append(ch)
            i += 1
            continue
        if visible >= n_visible:
            if inside_marker:
                result.append('*')
            break
        result.append(ch)
        visible += 1
        i += 1
    return ''.join(result)

def visible_len(caption: str) -> int:
    return len(caption.replace('*', ''))

def get_partial_caption_words(caption: str, n_words: int) -> str:
    """Return caption with only first n_words words visible, preserving *marker* syntax."""
    result = []
    words_shown = 0
    inside_marker = False
    in_word = False
    for ch in caption:
        if ch == '*':
            inside_marker = not inside_marker
            result.append(ch)
            continue
        if ch == ' ':
            if in_word:
                words_shown += 1
                in_word = False
                if words_shown >= n_words:
                    if inside_marker:
                        result.append('*')
                    break
            result.append(ch)
        else:
            in_word = True
            result.append(ch)
    return ''.join(result)

# ─── Text frame rendering ─────────────────────────────────────────────────────

def wrap_parts(parts: list, font, max_width: int) -> list:
    lines = []
    current_line = []
    current_width = 0
    space_w = font.getlength(" ")

    for part in parts:
        words = part["text"].split(" ")
        for word in words:
            if not word:
                continue
            word_w = font.getlength(word)
            gap = space_w if current_line else 0
            if current_width + gap + word_w > max_width and current_line:
                lines.append(current_line)
                current_line = [{"text": word, "highlight": part["highlight"]}]
                current_width = word_w
            else:
                if current_line and current_line[-1]["highlight"] == part["highlight"]:
                    current_line[-1]["text"] += (" " if current_line[-1]["text"] else "") + word
                else:
                    if current_line:
                        current_line[-1]["text"] += " "
                    current_line.append({"text": word, "highlight": part["highlight"]})
                current_width += gap + word_w

    if current_line:
        lines.append(current_line)
    return lines


def render_text_frame(caption, w, h, font_path, font_size, text_color,
                      hi_color, hi_mode, pos_x, pos_y, max_chars, pill_padding=8):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = load_font(font_path, font_size)

    parts = parse_caption(caption)
    max_width = w - 100
    lines = wrap_parts(parts, font, max_width)
    line_height = font_size + 14
    total_h = len(lines) * line_height

    block_y = (h - total_h) // 2 if pos_y == "center" else int(pos_y) - total_h // 2
    tc = hex_to_rgb(text_color) + (255,)
    hc = hex_to_rgb(hi_color) + (255,)

    for line in lines:
        line_w = 0
        for i, span in enumerate(line):
            if i > 0:
                line_w += int(font.getlength(" "))
            line_w += int(font.getlength(span["text"]))

        x = (w - line_w) // 2 if pos_x == "center" else int(pos_x) - line_w // 2

        for i, span in enumerate(line):
            if i > 0:
                x += int(font.getlength(" "))
            sw = int(font.getlength(span["text"]))

            if span["highlight"] and hi_mode == "pill":
                pad = pill_padding
                pill = [x - pad, block_y - pad // 2,
                        x + sw + pad, block_y + font_size + pad // 2]
                draw.rounded_rectangle(pill, radius=6, fill=hc)
                draw.text((x, block_y), span["text"], font=font, fill=tc)
            elif span["highlight"] and hi_mode == "text":
                draw.text((x, block_y), span["text"], font=font, fill=hc)
            else:
                draw.text((x, block_y), span["text"], font=font, fill=tc)

            x += sw

        block_y += line_height

    return img

# ─── Typewriter frame sequence ────────────────────────────────────────────────

def render_typewriter_frames(caption, duration, w, h, font_path, font_size,
                              text_color, hi_color, hi_mode, pos_x, pos_y,
                              max_chars, pill_padding, frames_dir, cap_cfg_speed=20):
    os.makedirs(frames_dir, exist_ok=True)

    total_frames = max(1, int(duration * FPS))
    n_chars = visible_len(caption)

    last_chars_shown = -1
    last_frame_path = None

    # Guarantee all characters are visible for at least HOLD_S seconds before the end.
    # If the configured speed is too slow to finish in time, auto-increase it.
    HOLD_S = 0.4
    type_time = max(0.1, duration - HOLD_S)
    min_speed = n_chars / type_time if n_chars else 1
    chars_per_sec = max(cap_cfg_speed if cap_cfg_speed else 20, min_speed)

    for frame_num in range(total_frames):
        chars_to_show = min(n_chars, int(frame_num / FPS * chars_per_sec) + 1)
        chars_to_show = max(1, chars_to_show)
        frame_path = os.path.join(frames_dir, f"frame_{frame_num:05d}.png")

        if chars_to_show != last_chars_shown:
            partial = get_partial_caption(caption, chars_to_show)
            img = render_text_frame(
                caption=partial, w=w, h=h,
                font_path=font_path, font_size=font_size,
                text_color=text_color, hi_color=hi_color, hi_mode=hi_mode,
                pos_x=pos_x, pos_y=pos_y,
                max_chars=max_chars, pill_padding=pill_padding
            )
            img.save(frame_path, "PNG")
            last_chars_shown = chars_to_show
            last_frame_path = frame_path
        else:
            shutil.copy(last_frame_path, frame_path)

    return frames_dir


def render_wordbyword_frames(caption, duration, w, h, font_path, font_size,
                              text_color, hi_color, hi_mode, pos_x, pos_y,
                              max_chars, pill_padding, frames_dir, cap_cfg_speed=3):
    os.makedirs(frames_dir, exist_ok=True)

    total_frames = max(1, int(duration * FPS))
    n_words = len(caption.replace('*', '').split())

    HOLD_S = 0.4
    type_time = max(0.1, duration - HOLD_S)
    min_speed = n_words / type_time if n_words else 1
    words_per_sec = max(cap_cfg_speed if cap_cfg_speed else 3, min_speed)

    last_words_shown = -1
    last_frame_path = None

    for frame_num in range(total_frames):
        words_to_show = min(n_words, int(frame_num / FPS * words_per_sec) + 1)
        words_to_show = max(1, words_to_show)
        frame_path = os.path.join(frames_dir, f"frame_{frame_num:05d}.png")

        if words_to_show != last_words_shown:
            partial = get_partial_caption_words(caption, words_to_show)
            img = render_text_frame(
                caption=partial, w=w, h=h,
                font_path=font_path, font_size=font_size,
                text_color=text_color, hi_color=hi_color, hi_mode=hi_mode,
                pos_x=pos_x, pos_y=pos_y,
                max_chars=max_chars, pill_padding=pill_padding
            )
            img.save(frame_path, "PNG")
            last_words_shown = words_to_show
            last_frame_path = frame_path
        else:
            shutil.copy(last_frame_path, frame_path)

    return frames_dir


# ─── Segment renderer ─────────────────────────────────────────────────────────

def render_segment(seg, preset, audio_path, output_path, w, h, work_dir):
    import time as _time
    raw_duration = round(seg["end"] - seg["start"], 3)
    if raw_duration <= 0:
        print(f"[segment {seg['index']}] WARNING: non-positive duration {raw_duration}s (start={seg['start']}, end={seg['end']}), clamping to 0.001")
    duration = max(raw_duration, 0.001)
    has_visual = seg["type"] == "visual" and seg.get("visual_path")
    show_caption = seg.get("show_caption", True)
    mute_audio   = seg.get("mute_audio", False)
    cap_cfg = preset["caption"]["with_visual"] if has_visual else preset["caption"]["without_visual"]
    bg = preset["background"]
    anim = cap_cfg.get("animation", "none")

    print(f"[segment {seg['index']}] start={seg['start']:.2f}s end={seg['end']:.2f}s "
          f"duration={duration:.2f}s type={seg['type']} has_visual={has_visual} "
          f"show_caption={show_caption} mute_audio={mute_audio} anim={anim}")

    font_path    = preset["caption"].get("font") or ""
    font_size    = cap_cfg["font_size"]
    text_color   = preset["caption"]["text_color"]
    hi_color     = preset["caption"]["highlight"]["color"]
    hi_mode      = preset["caption"]["highlight"]["mode"]
    pill_padding = preset["caption"]["highlight"].get("pill_padding", 8)
    pos          = cap_cfg.get("position", {})
    pos_x        = pos.get("x", "center")
    pos_y        = pos.get("y", "center")
    max_chars    = cap_cfg.get("max_chars_per_line", 18)

    # ── pre-render text frames (Pillow) ──────────────────────────────────
    # Done before building the FFmpeg command so we know what to pass as input
    text_ffmpeg_input = []
    text_cleanup = None
    if show_caption:
        if anim == "typing":
            frames_dir = os.path.join(work_dir, f"tw_{seg['index']}")
            print(f"[segment {seg['index']}] pre-rendering typewriter frames → {frames_dir}")
            _tw_t0 = _time.time()
            render_typewriter_frames(
                caption=seg["caption"], duration=duration,
                w=w, h=h, font_path=font_path, font_size=font_size,
                text_color=text_color, hi_color=hi_color, hi_mode=hi_mode,
                pos_x=pos_x, pos_y=pos_y, max_chars=max_chars,
                pill_padding=pill_padding, frames_dir=frames_dir,
                cap_cfg_speed=cap_cfg.get("animation_speed", 20)
            )
            text_ffmpeg_input = ["-framerate", str(FPS), "-i",
                                 os.path.join(frames_dir, "frame_%05d.png")]
            text_cleanup = frames_dir
            print(f"[segment {seg['index']}] typewriter frames done in {_time.time()-_tw_t0:.1f}s")
        elif anim == "wordbyword":
            frames_dir = os.path.join(work_dir, f"ww_{seg['index']}")
            print(f"[segment {seg['index']}] pre-rendering word-by-word frames → {frames_dir}")
            _tw_t0 = _time.time()
            render_wordbyword_frames(
                caption=seg["caption"], duration=duration,
                w=w, h=h, font_path=font_path, font_size=font_size,
                text_color=text_color, hi_color=hi_color, hi_mode=hi_mode,
                pos_x=pos_x, pos_y=pos_y, max_chars=max_chars,
                pill_padding=pill_padding, frames_dir=frames_dir,
                cap_cfg_speed=cap_cfg.get("animation_speed", 3)
            )
            text_ffmpeg_input = ["-framerate", str(FPS), "-i",
                                 os.path.join(frames_dir, "frame_%05d.png")]
            text_cleanup = frames_dir
            print(f"[segment {seg['index']}] word-by-word frames done in {_time.time()-_tw_t0:.1f}s")
        else:
            text_png = os.path.join(work_dir, f"text_{seg['index']}.png")
            print(f"[segment {seg['index']}] pre-rendering static text PNG → {text_png}")
            img = render_text_frame(
                caption=seg["caption"], w=w, h=h,
                font_path=font_path, font_size=font_size,
                text_color=text_color, hi_color=hi_color, hi_mode=hi_mode,
                pos_x=pos_x, pos_y=pos_y, max_chars=max_chars,
                pill_padding=pill_padding
            )
            img.save(text_png, "PNG")
            text_ffmpeg_input = ["-loop", "1", "-t", str(duration + 1), "-i", text_png]
            text_cleanup = text_png

    # ── build inputs list and filter graph IN ORDER ───────────────────────
    # Rule: every time we append to all_inputs, we note the index,
    # then immediately write the filter for that input.
    # This guarantees [N:v] always refers to the correct input.

    all_inputs = []
    filters = []
    idx = 0

    # Input 0: background
    if bg["type"] == "color":
        color = bg["color"].lstrip("#")
        all_inputs += ["-f", "lavfi", "-i",
                       f"color=c=#{color}:size={w}x{h}:rate={FPS}:duration={duration+1}"]
        filters.append(f"[{idx}:v]trim=duration={duration},setpts=PTS-STARTPTS,"
                       f"format=yuv420p,setsar=1[bg]")
    elif bg["type"] == "image":
        all_inputs += ["-loop", "1", "-t", str(duration+1), "-i", bg["file"]]
        filters.append(f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                       f"crop={w}:{h},trim=duration={duration},setpts=PTS-STARTPTS,"
                       f"format=yuv420p,setsar=1[bg]")
    else:  # video
        all_inputs += ["-stream_loop", "-1", "-t", str(duration+1), "-i", bg["file"]]
        filters.append(f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                       f"crop={w}:{h},trim=duration={duration},setpts=PTS-STARTPTS,"
                       f"format=yuv420p,setsar=1[bg]")
    idx += 1
    current = "bg"

    # Input 1+: frame (optional, behind visual), then visual on top
    if has_visual:
        # frame overlay first so it sits behind the visual
        fc = preset.get("frame", {})
        if fc.get("enabled") and fc.get("file"):
            fcon = fc["container"]
            fx, fy, fw, fh = fcon["x"], fcon["y"], fcon["w"], fcon["h"]
            is_img = Path(fc["file"]).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            if is_img:
                all_inputs += ["-loop", "1", "-t", str(duration+1), "-i", fc["file"]]
            else:
                all_inputs += ["-stream_loop", "-1", "-t", str(duration+1), "-i", fc["file"]]
            filters.append(
                f"[{idx}:v]scale={fw}:{fh}:force_original_aspect_ratio=increase,"
                f"crop={fw}:{fh},trim=duration={duration},setpts=PTS-STARTPTS,"
                f"format=rgba,setsar=1[frame{idx}]"
            )
            filters.append(f"[{current}][frame{idx}]overlay={fx}:{fy}[after_frame]")
            current = "after_frame"
            idx += 1

        # visual overlay on top of frame
        vis = preset["visual"]
        vc = vis["container"]
        vx, vy, vw, vh = vc["x"], vc["y"], vc["w"], vc["h"]
        _vis_is_img = Path(seg["visual_path"]).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        if _vis_is_img:
            all_inputs += ["-loop", "1", "-t", str(duration+1), "-i", seg["visual_path"]]
        else:
            all_inputs += ["-stream_loop", "-1", "-t", str(duration+1), "-i", seg["visual_path"]]
        _vis_mode = seg.get("visual_mode", "crop")
        if _vis_mode == "fit":
            vf = (f"[{idx}:v]scale={vw}:{vh}:force_original_aspect_ratio=decrease,"
                  f"pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:color=black,"
                  f"trim=duration={duration},setpts=PTS-STARTPTS,format=yuv420p,setsar=1")
        else:
            vf = (f"[{idx}:v]scale={vw}:{vh}:force_original_aspect_ratio=increase,"
                  f"crop={vw}:{vh},"
                  f"trim=duration={duration},setpts=PTS-STARTPTS,format=yuv420p,setsar=1")
        if vis.get("animation", "none") == "fade_in":
            d = vis.get("animation_duration", 0.5)
            vf += f",fade=t=in:st=0:d={d}"
        vf += f"[vis{idx}]"
        filters.append(vf)
        filters.append(f"[{current}][vis{idx}]overlay={vx}:{vy}[after_vis]")
        current = "after_vis"
        idx += 1

    # Next input: text (image sequence or static PNG)
    if show_caption:
        all_inputs += text_ffmpeg_input
        if anim in ("typing", "wordbyword"):
            filters.append(
                f"[{idx}:v]trim=duration={duration},setpts=PTS-STARTPTS,"
                f"format=rgba,setsar=1[text{idx}]"
            )
        elif anim == "fade":
            d = cap_cfg.get("animation_duration", 0.4)
            filters.append(
                f"[{idx}:v]fade=t=in:st=0:d={d}:alpha=1,"
                f"trim=duration={duration},setpts=PTS-STARTPTS,"
                f"format=rgba,setsar=1[text{idx}]"
            )
        else:
            filters.append(
                f"[{idx}:v]trim=duration={duration},setpts=PTS-STARTPTS,"
                f"format=rgba,setsar=1[text{idx}]"
            )
        # convert base to rgba before overlaying transparent text
        filters.append(f"[{current}]format=rgba[base_rgba]")
        filters.append(f"[base_rgba][text{idx}]overlay=0:0:format=auto[after_text]")
        current = "after_text"
        idx += 1

    # transition fade out
    trans = preset.get("transitions", {})
    if trans.get("type") == "fade":
        td = trans.get("duration", 0.3)
        fade_start = max(0, duration - td)
        filters.append(f"[{current}]fade=t=out:st={fade_start}:d={td}[faded]")
        current = "faded"

    # final: back to yuv420p for H.264
    filters.append(f"[{current}]format=yuv420p[final_fmt]")
    current = "final_fmt"

    # audio
    if mute_audio:
        all_inputs += ["-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo"]
    else:
        audio_start = max(0, seg["start"] - AUDIO_PAD)
        audio_dur = duration + AUDIO_PAD * 2
        all_inputs += ["-ss", str(audio_start), "-t", str(audio_dur), "-i", audio_path]
    audio_idx = idx

    cmd = [
        "ffmpeg", "-y",
        *all_inputs,
        "-filter_complex", ";".join(filters),
        "-map", f"[{current}]",
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-ar", "44100",
        "-t", str(duration),
        "-r", str(FPS),
        output_path
    ]
    print(f"[segment {seg['index']}] running ffmpeg ({len(all_inputs)//2} inputs, "
          f"{len(filters)} filters) → {output_path}")
    _ffmpeg_t0 = _time.time()
    run(cmd)
    print(f"[segment {seg['index']}] ffmpeg done in {_time.time()-_ffmpeg_t0:.1f}s")

    # cleanup
    try:
        if os.path.isdir(text_cleanup):
            shutil.rmtree(text_cleanup)
        elif os.path.isfile(text_cleanup):
            os.remove(text_cleanup)
    except:
        pass


# ─── Main pipeline ────────────────────────────────────────────────────────────

def render_video(data: dict, preset: dict, output_path: str, progress_cb=None):
    import time as _time
    res = preset.get("resolution", {"w": 1080, "h": 1920})
    w, h = res["w"], res["h"]
    work_dir = output_path.replace(".mp4", "_work")
    os.makedirs(work_dir, exist_ok=True)

    segments = data["segments"]
    audio_path = data["audio_path"]
    n_segs = len(segments)

    bgm_cfg  = preset.get("bgm", {})
    bgm_file = bgm_cfg.get("file")
    has_bgm  = bool(bgm_file and os.path.exists(bgm_file))
    total_steps = n_segs + 1 + (1 if has_bgm else 0)
    step = 0

    print(f"[render] Starting — {n_segs} segments, output: {output_path}")

    def _progress(stage: str, msg: str):
        nonlocal step
        step += 1
        print(f"[render] [{step}/{total_steps}] {stage}: {msg}")
        if progress_cb:
            progress_cb(step, total_steps, stage, msg)

    segment_files = []
    intro_inserted = False
    render_start = _time.time()

    for i, seg in enumerate(segments):
        seg_out = os.path.join(work_dir, f"seg_{i:03d}.mp4")
        t0 = _time.time()
        print(f"[render]   segment {i+1}/{n_segs} — start={seg['start']:.2f}s end={seg['end']:.2f}s caption={seg.get('caption','')[:40]!r}")

        intro_duration = 0.0
        if seg.get("is_intro_pin") and data.get("intro_path") and not intro_inserted:
            print(f"[render]   rendering intro clip …")
            intro_out = os.path.join(work_dir, "intro_clip.mp4")
            intro_duration = get_video_duration(data["intro_path"])
            audio_start = max(0, seg["start"] - AUDIO_PAD)
            run([
                "ffmpeg", "-y",
                "-i", data["intro_path"],
                "-ss", str(audio_start), "-i", audio_path,
                "-filter_complex",
                f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[vout]",
                "-map", "[vout]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-ar", "44100",
                "-shortest", intro_out
            ])
            segment_files.append(intro_out)
            intro_inserted = True
            print(f"[render]   intro clip done ({_time.time()-t0:.1f}s)")

        effective_seg = seg if intro_duration == 0.0 else {**seg, "start": seg["start"] + intro_duration}
        render_segment(effective_seg, preset, audio_path, seg_out, w, h, work_dir)
        segment_files.append(seg_out)
        elapsed = _time.time() - t0
        print(f"[render]   segment {i+1} done in {elapsed:.1f}s")
        _progress("render", f"Segment {i + 1} of {n_segs}")

    if data.get("outro_path") and os.path.exists(data["outro_path"]):
        print(f"[render]   rendering outro clip …")
        outro_out = os.path.join(work_dir, "outro_clip.mp4")
        run([
            "ffmpeg", "-y", "-i", data["outro_path"],
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", outro_out
        ])
        segment_files.append(outro_out)
        print(f"[render]   outro clip done")

    print(f"[render] Concatenating {len(segment_files)} clips …")
    _progress("concat", "Concatenating clips…")
    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for sf in segment_files:
            f.write(f"file '{os.path.abspath(sf)}'\n")

    concat_out = os.path.join(work_dir, "concat_raw.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", concat_list, "-c", "copy", concat_out])
    print(f"[render] Concat done")

    if has_bgm:
        print(f"[render] Mixing BGM …")
        _progress("bgm", "Mixing BGM…")
        bgm_vol   = bgm_cfg.get("bgm_volume", 0.15)
        voice_vol = bgm_cfg.get("voice_volume", 1.0)
        fade_out  = bgm_cfg.get("fade_out", 2.0)
        total_dur = sum(s["end"] - s["start"] for s in segments)
        fade_start = max(0, total_dur - fade_out)
        run([
            "ffmpeg", "-y",
            "-i", concat_out,
            "-stream_loop", "-1", "-i", bgm_file,
            "-filter_complex",
            f"[0:a]volume={voice_vol}[voice];"
            f"[1:a]volume={bgm_vol},afade=t=out:st={fade_start}:d={fade_out},"
            f"atrim=duration={total_dur}[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=first[audio_out]",
            "-map", "0:v", "-map", "[audio_out]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "44100",
            output_path
        ])
        print(f"[render] BGM mix done")
    else:
        shutil.copy(concat_out, output_path)

    total_elapsed = _time.time() - render_start
    print(f"[render] Done — total time: {total_elapsed:.1f}s → {output_path}")

    try:
        shutil.rmtree(work_dir)
    except:
        pass
