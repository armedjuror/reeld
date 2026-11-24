"""Standalone app for the suite: API + the task-first UI at /.

    uvicorn suite.app:app --reload --port 8100

The legacy editor (main.py) also mounts the same router and serves the UI at
/studio, so either entry point works.
"""

from __future__ import annotations

from pathlib import Path

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware

from .api import router
from .auth import router as auth_router

UI = Path(__file__).resolve().parent.parent / "static" / "suite.html"

app = FastAPI(title="reeld suite")
# Same cookie and secret as the editor, so one sign-in works on both.
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
                   max_age=60 * 60 * 24 * 30)
app.include_router(router)
app.include_router(auth_router)


@app.get("/", include_in_schema=False)
@app.get("/studio", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(UI)
