"""Sign-in for the studio with a static key.

Set REELD_AUTH_KEY in .env. The studio then asks for that key once per
browser and keeps you signed in for 30 days (signed session cookie, secret
SESSION_SECRET). Without REELD_AUTH_KEY the studio is open: fine on your own
Mac, never expose it like that.

The signed-in account is the local user (REELD_USER_EMAIL, or the only user).
Media URLs stay public by id so SuperProfile can fetch videos.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .db import session_scope
from .users import UserNotConfigured, current_user_id

router = APIRouter(prefix="/api/suite/auth", tags=["suite-auth"])
SESSION_KEY = "suite_user_id"  # separate from the editor's Google session


def auth_key() -> str:
    return os.environ.get("REELD_AUTH_KEY", "").strip()


def login_required() -> bool:
    return bool(auth_key())


def session_user_id(request: Request) -> Optional[int]:
    if "session" not in request.scope:
        return None
    uid = request.session.get(SESSION_KEY)
    return int(uid) if uid else None


class LoginIn(BaseModel):
    key: str


@router.post("/login")
def login(body: LoginIn, request: Request) -> dict:
    expected = auth_key()
    if not expected:
        return {"ok": True, "mode": "open"}
    if not hmac.compare_digest(body.key.strip().encode(), expected.encode()):
        time.sleep(1.0)  # slow down guessing
        raise HTTPException(401, "That key isn't right")
    with session_scope() as s:
        try:
            uid = current_user_id(s)
        except UserNotConfigured as exc:
            raise HTTPException(503, str(exc)) from exc
    request.session[SESSION_KEY] = uid
    return {"ok": True}


@router.post("/logout")
def logout(request: Request) -> dict:
    if "session" in request.scope:
        request.session.pop(SESSION_KEY, None)
    return {"ok": True}
