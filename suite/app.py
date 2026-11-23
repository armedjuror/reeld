"""Standalone app for the suite: API + the task-first UI at /.

    uvicorn suite.app:app --reload --port 8100

The legacy editor (main.py) also mounts the same router and serves the UI at
/studio, so either entry point works.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .api import router

UI = Path(__file__).resolve().parent.parent / "static" / "suite.html"

app = FastAPI(title="reeld suite")
app.include_router(router)


@app.get("/", include_in_schema=False)
@app.get("/studio", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(UI)
