"""
Storage abstraction — local filesystem or AWS S3.
Controlled by the STORAGE env var:
  STORAGE=local   (default)  – saves to local disk
  STORAGE=s3                 – saves to / serves from S3

Required env vars for S3:
  S3_BUCKET, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""

import os
import copy
import shutil
import tempfile
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

STORAGE_TYPE = os.environ.get("STORAGE", "local").lower()

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")

# ─── S3 client (lazy) ─────────────────────────────────────────────────────────

def _s3():
    import boto3
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
    )

# ─── Core operations ──────────────────────────────────────────────────────────

def save_upload(file_obj, folder: str, ext: str) -> str:
    """Save an uploaded file object. Returns stored path/key."""
    key = f"{folder}/{uuid.uuid4()}{ext}"
    if STORAGE_TYPE == "s3":
        _s3().upload_fileobj(file_obj, S3_BUCKET, key)
    else:
        os.makedirs(folder, exist_ok=True)
        with open(key, "wb") as f:
            shutil.copyfileobj(file_obj, f)
    return key


def save_local(local_path: str, dest_key: str) -> str:
    """Upload an already-local file to storage. Returns the key."""
    if STORAGE_TYPE == "s3":
        _s3().upload_file(local_path, S3_BUCKET, dest_key)
    return dest_key


def delete(path: str):
    """Delete a stored file. Safe to call with None."""
    if not path:
        return
    if STORAGE_TYPE == "s3":
        try:
            _s3().delete_object(Bucket=S3_BUCKET, Key=path)
        except Exception:
            pass
    else:
        try:
            os.remove(path)
        except Exception:
            pass


def exists(path: str) -> bool:
    """Check if a stored file exists."""
    if not path:
        return False
    if STORAGE_TYPE == "s3":
        try:
            _s3().head_object(Bucket=S3_BUCKET, Key=path)
            return True
        except Exception:
            return False
    return os.path.exists(path)


def presigned_url(key: str, expires: int = 3600) -> str:
    """Return a presigned GET URL for an S3 object."""
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )


def localize(path: str, temp_dir: str) -> str:
    """Return a guaranteed-local path. Downloads from S3 if needed.
    No-op (returns path unchanged) for local storage or empty path."""
    if not path:
        return path
    if STORAGE_TYPE == "s3":
        ext = Path(path).suffix
        local = os.path.join(temp_dir, f"{uuid.uuid4()}{ext}")
        _s3().download_file(S3_BUCKET, path, local)
        return local
    return path

# ─── Render helpers ───────────────────────────────────────────────────────────

def localize_for_render(render_data: dict, preset: dict, temp_dir: str):
    """
    For S3 storage: download every file referenced in render_data and preset
    to temp_dir and return (localized_render_data, localized_preset).
    For local storage: returns the originals unchanged.
    """
    if STORAGE_TYPE != "s3":
        return render_data, preset

    rd = copy.deepcopy(render_data)
    p  = copy.deepcopy(preset)

    rd["audio_path"] = localize(rd.get("audio_path"), temp_dir)
    rd["intro_path"] = localize(rd.get("intro_path"), temp_dir)
    rd["outro_path"] = localize(rd.get("outro_path"), temp_dir)
    for seg in rd.get("segments", []):
        if seg.get("visual_path"):
            seg["visual_path"] = localize(seg["visual_path"], temp_dir)

    if p.get("background", {}).get("file"):
        p["background"]["file"] = localize(p["background"]["file"], temp_dir)
    if p.get("bgm", {}).get("file"):
        p["bgm"]["file"] = localize(p["bgm"]["file"], temp_dir)
    if p.get("caption", {}).get("font"):
        p["caption"]["font"] = localize(p["caption"]["font"], temp_dir)
    if p.get("frame", {}).get("file"):
        p["frame"]["file"] = localize(p["frame"]["file"], temp_dir)

    return rd, p
