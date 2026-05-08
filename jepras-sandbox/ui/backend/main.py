"""FastAPI dev UI backend for jepras-sandbox YAML agents.

Streams raw ``Event`` objects over SSE so a React client can render every
event type (text deltas, tool calls, tool responses, thinking blocks,
transfers) with part-level fidelity.

Run locally::

    uvicorn main:app --reload --app-dir jepras-sandbox/ui/backend --port 8000
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from orxhestra.composer.composer import Composer
from orxhestra.runner import Runner

SANDBOX_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SANDBOX_ROOT.parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

app = FastAPI(title="orxhestra sandbox UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_RUNNER_CACHE: dict[str, Runner] = {}


def _resolve_yaml(relpath: str) -> Path:
    abs_path = (SANDBOX_ROOT / relpath).resolve()
    try:
        abs_path.relative_to(SANDBOX_ROOT)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="yaml path escapes sandbox") from e
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"yaml not found: {relpath}")
    return abs_path


async def _get_runner(relpath: str) -> Runner:
    abs_path = _resolve_yaml(relpath)
    key = str(abs_path)
    if key not in _RUNNER_CACHE:
        _RUNNER_CACHE[key] = await Composer.runner_from_yaml_async(abs_path)
    return _RUNNER_CACHE[key]


class ConfigEntry(BaseModel):
    name: str
    relpath: str


@app.get("/api/configs", response_model=list[ConfigEntry])
def list_configs() -> list[ConfigEntry]:
    entries: list[ConfigEntry] = []
    for path in sorted(SANDBOX_ROOT.glob("*.yaml")):
        entries.append(ConfigEntry(name=path.name, relpath=path.name))
    for path in sorted(SANDBOX_ROOT.glob("*/orx.yaml")):
        rel = path.relative_to(SANDBOX_ROOT).as_posix()
        if path.parent.name.startswith("ui"):
            continue
        entries.append(ConfigEntry(name=rel, relpath=rel))
    return entries


class StreamRequest(BaseModel):
    yaml: str
    session_id: str
    user_id: str
    prompt: str


@app.post("/api/stream")
async def stream(req: StreamRequest) -> StreamingResponse:
    runner = await _get_runner(req.yaml)

    async def gen():
        try:
            async for event in runner.astream(
                user_id=req.user_id,
                session_id=req.session_id,
                new_message=req.prompt,
            ):
                yield f"data: {event.model_dump_json()}\n\n"
            yield 'data: {"done": true}\n\n'
        except Exception as e:
            err = json.dumps({"error": f"{type(e).__name__}: {e}"})
            yield f"data: {err}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{yaml_name:path}/{session_id}/events")
async def session_events(yaml_name: str, session_id: str) -> list[dict[str, Any]]:
    runner = await _get_runner(yaml_name)
    session = await runner.session_service.get_session(
        app_name=runner.app_name,
        user_id="sandbox-user",
        session_id=session_id,
    )
    if session is None:
        return []
    return [e.model_dump(mode="json") for e in session.events]


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "sandbox": str(SANDBOX_ROOT)}
