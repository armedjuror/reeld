import os
import httpx
from fastapi import Request, HTTPException
from urllib.parse import urlencode

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI",
                                       "http://localhost:8888/auth/google/callback")

_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL   = "https://oauth2.googleapis.com/token"
_USERINFO    = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_auth_url() -> str:
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "prompt":        "select_account",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Exchange OAuth code → Google user-info dict {sub, email, name, picture}."""
    async with httpx.AsyncClient() as client:
        tok = await client.post(_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
        tok.raise_for_status()
        info = await client.get(_USERINFO, headers={
            "Authorization": f"Bearer {tok.json()['access_token']}"
        })
        info.raise_for_status()
        return info.json()


async def require_auth(request: Request) -> int:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not authenticated")
    return int(uid)
