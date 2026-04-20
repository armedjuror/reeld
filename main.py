from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn, shutil, uuid, os, re, subprocess, json as _json, threading, queue, asyncio
import math, hmac, hashlib, tempfile
import razorpay
import storage

from auth import require_auth, exchange_code, google_auth_url
from db import (init_db, upsert_user, get_user_by_id,
                list_presets, get_preset, create_preset, update_preset,
                delete_preset, get_preset_by_share_token,
                create_reel, get_reel, list_reels, update_reel,
                delete_reel, log_generation, get_generations_for_reel,
                get_credit_balance, add_credits, deduct_credits,
                get_credit_transactions, CREDIT_PRICE_PAISE)
from transcribe import transcribe_audio_local
from render import render_video

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
    max_age=60 * 60 * 24 * 30,  # 30 days
)

TEMP_DIR   = "temp"
OUTPUT_DIR = "outputs"
REEL_DIR   = "reel_assets"
for d in (TEMP_DIR, OUTPUT_DIR, REEL_DIR):
    os.makedirs(d, exist_ok=True)

# job_id → queue.Queue of SSE event dicts
_render_jobs: dict[str, queue.Queue] = {}

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def landing_page():
    return FileResponse("static/landing.html")

# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/auth/google")
def google_login():
    return RedirectResponse(google_auth_url())

@app.get("/auth/google/callback")
async def google_callback(code: str, request: Request):
    try:
        info = await exchange_code(code)
    except Exception as e:
        raise HTTPException(400, f"OAuth failed: {e}")
    user = upsert_user(
        google_id  = info["sub"],
        email      = info["email"],
        name       = info.get("name", ""),
        avatar_url = info.get("picture", ""),
    )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/")

@app.get("/auth/me")
async def auth_me(user_id: int = Depends(require_auth)):
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user["created_at"] = str(user["created_at"])
    return user

@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

# ─── Presets ─────────────────────────────────────────────────────────────────

@app.get("/api/presets")
def list_presets_route(user_id: int = Depends(require_auth)):
    return list_presets(user_id)

@app.get("/api/presets/{preset_id}")
def get_one_preset(preset_id: str, user_id: int = Depends(require_auth)):
    p = get_preset(preset_id, user_id)
    if not p:
        raise HTTPException(404, "Preset not found")
    return p

@app.post("/api/presets")
async def create_preset_route(data: dict, user_id: int = Depends(require_auth)):
    return create_preset(user_id, data)

@app.put("/api/presets/{preset_id}")
async def update_preset_route(preset_id: str, data: dict,
                               user_id: int = Depends(require_auth)):
    p = update_preset(preset_id, user_id, data)
    if not p:
        raise HTTPException(404, "Preset not found")
    return p

@app.delete("/api/presets/{preset_id}")
def remove_preset(preset_id: str, user_id: int = Depends(require_auth)):
    if not delete_preset(preset_id, user_id):
        raise HTTPException(404, "Preset not found")
    return {"ok": True}

@app.post("/api/presets/{preset_id}/share")
def share_preset(preset_id: str, request: Request,
                 user_id: int = Depends(require_auth)):
    p = get_preset(preset_id, user_id)
    if not p:
        raise HTTPException(404, "Preset not found")
    base = str(request.base_url).rstrip("/")
    return {"share_url": f"{base}/import/{p['share_token']}"}

@app.post("/api/presets/import")
async def import_preset(data: dict, user_id: int = Depends(require_auth)):
    token = data.get("token", "").strip()
    # Accept full URL or bare token
    token = token.split("/")[-1]
    source = get_preset_by_share_token(token)
    if not source:
        raise HTTPException(404, "Share link not found or expired")
    # Copy into current user's presets (strip id/share_token)
    copy = {k: v for k, v in source.items() if k not in ("id", "share_token")}
    copy["name"] = copy.get("name", "Imported Preset") + " (imported)"
    return create_preset(user_id, copy)

# ─── Preset asset uploads ─────────────────────────────────────────────────────

def _save_upload(file: UploadFile, folder: str) -> dict:
    ext  = os.path.splitext(file.filename)[1]
    path = storage.save_upload(file.file, folder, ext)
    return {"path": path}

@app.post("/api/upload/bgm")
async def upload_bgm(file: UploadFile = File(...)):
    return _save_upload(file, "bgm")

@app.post("/api/upload/frame")
async def upload_frame(file: UploadFile = File(...)):
    return _save_upload(file, "frames")

@app.post("/api/upload/background")
async def upload_background(file: UploadFile = File(...)):
    return _save_upload(file, "backgrounds")

@app.post("/api/upload/font")
async def upload_font(file: UploadFile = File(...)):
    result = _save_upload(file, "fonts")
    result["original_name"] = file.filename
    return result

# ─── Reel asset uploads (persistent) ─────────────────────────────────────────

@app.post("/api/reel/upload/audio")
async def upload_reel_audio(file: UploadFile = File(...)):
    return _save_upload(file, REEL_DIR)

@app.post("/api/reel/upload/intro")
async def upload_reel_intro(file: UploadFile = File(...)):
    return _save_upload(file, REEL_DIR)

@app.post("/api/reel/upload/outro")
async def upload_reel_outro(file: UploadFile = File(...)):
    return _save_upload(file, REEL_DIR)

@app.post("/api/reel/upload/visual")
async def upload_reel_visual(segment_index: int = Form(...), file: UploadFile = File(...)):
    return _save_upload(file, REEL_DIR)

# ─── Silence removal ─────────────────────────────────────────────────────────

def _remove_silence(audio_path: str, min_silence_s: float, padding_s: float) -> dict:
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
        capture_output=True, text=True, check=True
    )
    total_dur = float(_json.loads(probe.stdout)["format"]["duration"])

    det = subprocess.run(
        ["ffmpeg", "-i", audio_path, "-af",
         f"silencedetect=noise=-40dB:duration={min_silence_s}",
         "-f", "null", "-"],
        capture_output=True, text=True
    )
    starts = [float(x) for x in re.findall(r"silence_start: (\S+)", det.stderr)]
    ends   = [float(x) for x in re.findall(r"silence_end: (\S+)",   det.stderr)]
    if len(starts) > len(ends):
        ends.append(total_dur)

    silences = list(zip(starts, ends))

    if not silences:
        return {
            "cleaned_path": audio_path,
            "removed_count": 0,
            "original_duration": round(total_dur, 2),
            "cleaned_duration":  round(total_dur, 2),
        }

    kept, pos = [], 0.0
    for s_start, s_end in silences:
        seg_end = min(s_start + padding_s, total_dur)
        if seg_end > pos + 0.05:
            kept.append((round(pos, 4), round(seg_end, 4)))
        pos = max(pos, s_end - padding_s)
    if pos < total_dur - 0.05:
        kept.append((round(pos, 4), round(total_dur, 4)))
    if not kept:
        kept = [(0.0, total_dur)]

    n = len(kept)
    parts = [
        f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]"
        for i, (s, e) in enumerate(kept)
    ]
    parts.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]")

    out_path = f"{REEL_DIR}/cleaned_{uuid.uuid4()}.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path,
         "-filter_complex", ";".join(parts),
         "-map", "[out]",
         "-c:a", "libmp3lame", "-q:a", "2", out_path],
        capture_output=True, check=True
    )

    return {
        "cleaned_path": out_path,
        "removed_count": len(silences),
        "original_duration": round(total_dur, 2),
        "cleaned_duration":  round(sum(e - s for s, e in kept), 2),
    }

@app.post("/api/reel/remove-silence")
async def remove_silence_route(
    audio_path:     str   = Form(...),
    min_silence_s:  float = Form(default=1.0),
    padding_s:      float = Form(default=0.15),
):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_audio = storage.localize(audio_path, tmp)
            result = _remove_silence(local_audio, min_silence_s, padding_s)
            if storage.STORAGE_TYPE == "s3":
                if result["cleaned_path"] == local_audio:
                    result["cleaned_path"] = audio_path   # no change; return original key
                else:
                    dest_key = f"{REEL_DIR}/{uuid.uuid4()}.mp3"
                    storage.save_local(result["cleaned_path"], dest_key)
                    try: os.remove(result["cleaned_path"])
                    except: pass
                    result["cleaned_path"] = dest_key
        return result
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"FFmpeg error: {e.stderr.decode()}")
    except Exception as e:
        raise HTTPException(500, str(e))

# ─── Transcription ────────────────────────────────────────────────────────────

@app.post("/api/transcribe")
async def transcribe(audio_path: str = Form(...)):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_audio = storage.localize(audio_path, tmp)
            segments = transcribe_audio_local(local_audio)
        return {"segments": segments}
    except Exception as e:
        raise HTTPException(500, str(e))

# ─── Reels CRUD ───────────────────────────────────────────────────────────────

def _fmt_reel(r: dict) -> dict:
    r["file_exists"] = bool(r.get("last_output")) and storage.exists(r["last_output"])
    r["created_at"]  = str(r["created_at"])
    r["updated_at"]  = str(r["updated_at"])
    return r

@app.get("/api/reels")
def list_reels_route(user_id: int = Depends(require_auth)):
    return [_fmt_reel(r) for r in list_reels(user_id)]

@app.get("/api/reels/{reel_id}")
def get_reel_route(reel_id: int, user_id: int = Depends(require_auth)):
    r = get_reel(reel_id)
    if not r or r.get("user_id") != user_id:
        raise HTTPException(404, "Reel not found")
    return _fmt_reel(r)

@app.post("/api/reels")
async def create_reel_route(data: dict, user_id: int = Depends(require_auth)):
    preset = get_preset(data["preset_id"])
    if not preset:
        raise HTTPException(404, "Preset not found")
    reel = create_reel(
        name        = data["name"],
        preset_id   = data["preset_id"],
        preset_name = preset["name"],
        audio_path  = data["audio_path"],
        segments    = data.get("segments", []),
        user_id     = user_id,
        intro_path  = data.get("intro_path"),
        outro_path  = data.get("outro_path"),
    )
    return _fmt_reel(reel)

@app.put("/api/reels/{reel_id}")
async def update_reel_route(reel_id: int, data: dict,
                             user_id: int = Depends(require_auth)):
    existing = get_reel(reel_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(404, "Reel not found")

    allowed = {"name", "preset_id", "audio_path", "intro_path", "outro_path", "segments", "status"}
    fields  = {k: v for k, v in data.items() if k in allowed}
    if "preset_id" in fields:
        p = get_preset(fields["preset_id"])
        if p:
            fields["preset_name"] = p["name"]

    reel = update_reel(reel_id, **fields)
    if not reel:
        raise HTTPException(404, "Reel not found")
    return _fmt_reel(reel)

@app.delete("/api/reels/{reel_id}")
def delete_reel_route(reel_id: int, user_id: int = Depends(require_auth)):
    reel = get_reel(reel_id)
    if not reel or reel.get("user_id") != user_id:
        raise HTTPException(404, "Reel not found")
    delete_reel(reel_id)
    for path_field in ("audio_path", "intro_path", "outro_path", "last_output"):
        storage.delete(reel.get(path_field))
    for seg in reel.get("segments", []):
        storage.delete(seg.get("visual_path"))
    return {"ok": True}

# ─── Credits & Payments ───────────────────────────────────────────────────────

MIN_CREDITS = 100

def _rzp_client():
    key_id     = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    return razorpay.Client(auth=(key_id, key_secret))

def _verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "").encode()
    msg    = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.get("/api/credits/balance")
def credits_balance(user_id: int = Depends(require_auth)):
    user    = get_user_by_id(user_id)
    balance = get_credit_balance(user_id)
    return {
        "balance":           balance,
        "user_type":         user.get("type", "free"),
        "price_per_credit":  CREDIT_PRICE_PAISE / 100,   # in rupees
        "min_recharge":      MIN_CREDITS,
    }

@app.get("/api/credits/transactions")
def credits_transactions(user_id: int = Depends(require_auth)):
    return get_credit_transactions(user_id)

@app.post("/api/payments/create-order")
async def create_payment_order(data: dict, user_id: int = Depends(require_auth)):
    credits = int(data.get("credits", 0))
    if credits < MIN_CREDITS:
        raise HTTPException(400, f"Minimum recharge is {MIN_CREDITS} credits")

    amount_paise = credits * CREDIT_PRICE_PAISE
    try:
        client = _rzp_client()
        order  = client.order.create({
            "amount":   amount_paise,
            "currency": "INR",
            "receipt":  f"user_{user_id}_{uuid.uuid4().hex[:8]}",
            "notes":    {"user_id": str(user_id), "credits": str(credits)},
        })
    except Exception as e:
        raise HTTPException(500, f"Razorpay error: {e}")

    return {
        "order_id":   order["id"],
        "amount":     amount_paise,
        "currency":   "INR",
        "key_id":     os.environ.get("RAZORPAY_KEY_ID", ""),
        "credits":    credits,
    }

@app.post("/api/payments/verify")
async def verify_payment(data: dict, user_id: int = Depends(require_auth)):
    order_id   = data.get("order_id", "")
    payment_id = data.get("payment_id", "")
    signature  = data.get("signature", "")
    credits    = int(data.get("credits", 0))

    if not _verify_signature(order_id, payment_id, signature):
        raise HTTPException(400, "Invalid payment signature")

    tx = add_credits(
        user_id            = user_id,
        amount             = credits,
        description        = f"Recharge — {credits} credits (₹{credits * CREDIT_PRICE_PAISE / 100:.2f})",
        razorpay_payment_id = payment_id,
        razorpay_order_id  = order_id,
    )
    new_balance = get_credit_balance(user_id)
    return {"ok": True, "credits_added": credits, "balance": new_balance, "transaction": tx}

# ─── Render ───────────────────────────────────────────────────────────────────

@app.post("/api/reels/{reel_id}/render")
async def render_reel(reel_id: int, user_id: int = Depends(require_auth)):
    reel = get_reel(reel_id)
    if not reel or reel.get("user_id") != user_id:
        raise HTTPException(404, "Reel not found")

    preset = get_preset(reel["preset_id"])
    if not preset:
        raise HTTPException(404, "Preset not found")

    # ── Credit check ─────────────────────────────────────────────────────────
    user = get_user_by_id(user_id)
    credits_to_deduct = 0
    total_sec_ceil    = 0
    if user.get("type") != "pro":
        duration_secs  = sum(s["end"] - s["start"] for s in reel.get("segments", []))
        total_sec_ceil = math.ceil(duration_secs)
        last_charged   = int(reel.get("last_charged_seconds") or 0)
        credits_to_deduct = max(0, total_sec_ceil - last_charged)

        if credits_to_deduct > 0:
            balance = get_credit_balance(user_id)
            if balance < credits_to_deduct:
                raise HTTPException(402, {
                    "error":    "insufficient_credits",
                    "balance":  balance,
                    "required": credits_to_deduct,
                    "shortfall": credits_to_deduct - balance,
                })

    output_path   = f"{OUTPUT_DIR}/{uuid.uuid4()}.mp4"
    segment_count = len(reel.get("segments", []))
    duration_sec  = sum(s["end"] - s["start"] for s in reel.get("segments", []))
    render_data   = {
        "audio_path": reel["audio_path"],
        "intro_path": reel.get("intro_path"),
        "outro_path": reel.get("outro_path"),
        "segments":   reel["segments"],
    }

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _render_jobs[job_id] = q

    # Deduct credits before spawning thread (balance already confirmed above)
    if credits_to_deduct > 0:
        deduct_credits(
            user_id     = user_id,
            amount      = credits_to_deduct,
            description = f"Export: {reel['name']} ({total_sec_ceil}s total, {credits_to_deduct}s charged)",
            reel_id     = reel_id,
        )
        update_reel(reel_id, last_charged_seconds=float(total_sec_ceil))

    def _run():
        def progress_cb(step, total, stage, msg):
            q.put({"type": "progress", "step": step, "total": total,
                   "stage": stage, "message": msg})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local_rd, local_preset = storage.localize_for_render(render_data, preset, tmp)
                local_out = os.path.join(tmp, "output.mp4") if storage.STORAGE_TYPE == "s3" else output_path
                render_video(local_rd, local_preset, local_out, progress_cb=progress_cb)
                if storage.STORAGE_TYPE == "s3":
                    storage.save_local(local_out, output_path)
            log_generation(reel_id, preset["name"], segment_count, duration_sec, output_path)
            updated = update_reel(reel_id, last_output=output_path, status="exported")
            updated = _fmt_reel(updated)
            updated["file_exists"] = True
            q.put({"type": "done", "output_path": output_path, "reel": updated,
                   "credits_deducted": credits_to_deduct})
        except Exception as e:
            log_generation(reel_id, preset["name"], segment_count, duration_sec, "", "failed")
            print(str(e))
            q.put({"type": "error", "message": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/reels/{reel_id}/render-stream/{job_id}")
async def render_stream(reel_id: int, job_id: str,
                        user_id: int = Depends(require_auth)):
    q = _render_jobs.get(job_id)
    if not q:
        raise HTTPException(404, "Render job not found")

    async def event_gen():
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await loop.run_in_executor(None, lambda: q.get(timeout=300))
                yield f"data: {_json.dumps(item)}\n\n"
                if item.get("type") in ("done", "error"):
                    _render_jobs.pop(job_id, None)
                    break
        except Exception:
            _render_jobs.pop(job_id, None)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/reels/{reel_id}/generations")
def reel_generations(reel_id: int, user_id: int = Depends(require_auth)):
    reel = get_reel(reel_id)
    if not reel or reel.get("user_id") != user_id:
        raise HTTPException(404, "Reel not found")
    return get_generations_for_reel(reel_id)

# ─── Download ─────────────────────────────────────────────────────────────────

@app.get("/api/download")
def download(path: str):
    if storage.STORAGE_TYPE == "s3":
        return RedirectResponse(storage.presigned_url(path, expires=300))
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename="reel.mp4")

@app.get("/api/media/{path:path}")
def serve_media(path: str):
    if storage.STORAGE_TYPE == "s3":
        return RedirectResponse(storage.presigned_url(path))
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path)

# ─── Import preset page (SPA route) ──────────────────────────────────────────

@app.get("/import/{token}")
def import_page(token: str):
    return FileResponse("static/index.html")

# ─── Static ───────────────────────────────────────────────────────────────────

@app.get("/dashboard")
def index():
    return FileResponse("static/index.html")

@app.get("/legal")
def legal():
    return FileResponse("static/legal.html")

@app.get("/app.js")
def appjs():
    return FileResponse("static/app.js", media_type="application/javascript")

app.mount("/bgm",         StaticFiles(directory="bgm"),         name="bgm")
app.mount("/frames",      StaticFiles(directory="frames"),      name="frames")
app.mount("/backgrounds", StaticFiles(directory="backgrounds"), name="backgrounds")
app.mount("/outputs",     StaticFiles(directory="outputs"),     name="outputs")
app.mount("/reel_assets", StaticFiles(directory="reel_assets"), name="reel_assets")
app.mount("/",            StaticFiles(directory="static"),      name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8888, reload=True)
