"""Media storage for the suite, on top of the repo's storage.py (local or S3).

The database keeps only `MediaAsset` rows (key + metadata); bytes live in
storage. Local keys are paths relative to REELD_MEDIA_ROOT.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MediaAsset, MediaKind

MEDIA_ROOT = Path(os.environ.get("REELD_MEDIA_ROOT", "suite_media")).resolve()


def _storage_type() -> str:
    return os.environ.get("STORAGE", "local").lower()


def _s3():
    import storage  # repo root module

    return storage._s3(), storage.S3_BUCKET


def put_file(
    session: Session,
    *,
    user_id: int,
    local_path: str | Path,
    kind: MediaKind,
    episode_id: Optional[int] = None,
    duration_sec: Optional[float] = None,
    meta: Optional[dict] = None,
) -> MediaAsset:
    """Store a local file and record it. The local file is left in place."""
    local_path = Path(local_path)
    ext = local_path.suffix or ".bin"
    scope = f"ep{episode_id}" if episode_id else "library"
    key = f"suite/u{user_id}/{scope}/{kind.value}-{uuid.uuid4().hex[:12]}{ext}"
    if _storage_type() == "s3":
        client, bucket = _s3()
        client.upload_file(str(local_path), bucket, key)
    else:
        dest = MEDIA_ROOT / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)
    asset = MediaAsset(
        user_id=user_id,
        episode_id=episode_id,
        kind=kind,
        storage_key=key,
        mime=mimetypes.guess_type(local_path.name)[0],
        duration_sec=duration_sec,
        meta=meta or {},
    )
    session.add(asset)
    session.flush()
    return asset


def put_bytes(session: Session, *, user_id: int, data: bytes, filename: str, kind: MediaKind,
              episode_id: Optional[int] = None, meta: Optional[dict] = None) -> MediaAsset:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(filename).name
        path.write_bytes(data)
        return put_file(session, user_id=user_id, local_path=path, kind=kind,
                        episode_id=episode_id, meta=meta)


@contextmanager
def local_copy(asset: MediaAsset) -> Iterator[Path]:
    """A local path for reading the asset (downloads from S3 when needed)."""
    if _storage_type() == "s3":
        client, bucket = _s3()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(asset.storage_key).name
            client.download_file(bucket, asset.storage_key, str(path))
            yield path
    else:
        yield MEDIA_ROOT / asset.storage_key


def local_path(asset: MediaAsset) -> Optional[Path]:
    """Direct path for local storage (None on S3)."""
    return None if _storage_type() == "s3" else MEDIA_ROOT / asset.storage_key


def public_url(asset: MediaAsset, expires: int = 7 * 24 * 3600) -> Optional[str]:
    """A URL an outside service (SuperProfile) can fetch, if one exists.

    S3: a presigned URL. Local: REELD_PUBLIC_BASE_URL + the media route, only
    if that base URL is configured (e.g. a tunnel to the Mac).
    """
    if _storage_type() == "s3":
        client, bucket = _s3()
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": asset.storage_key}, ExpiresIn=expires
        )
    base = os.environ.get("REELD_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/api/suite/media/{asset.id}" if base else None


def latest(session: Session, episode_id: int, kind: MediaKind) -> Optional[MediaAsset]:
    return session.scalar(
        select(MediaAsset)
        .where(MediaAsset.episode_id == episode_id, MediaAsset.kind == kind)
        .order_by(MediaAsset.id.desc())
        .limit(1)
    )


def library(session: Session, user_id: int, kind: MediaKind) -> list[MediaAsset]:
    return list(
        session.scalars(
            select(MediaAsset)
            .where(MediaAsset.user_id == user_id, MediaAsset.episode_id.is_(None), MediaAsset.kind == kind)
            .order_by(MediaAsset.id)
        )
    )
