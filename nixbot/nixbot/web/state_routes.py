"""Hercules state API endpoints (see effects_state.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response

from nixbot.effects_state import TaskTokens, state_file_path

if TYPE_CHECKING:
    from pathlib import Path

MAX_STATE_SIZE = 16 * 1024 * 1024


def create_state_router(state_dir: Path, tokens: TaskTokens) -> APIRouter:
    router = APIRouter()

    def _project(request: Request) -> int:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        project_id = tokens.project_for(token) if scheme.lower() == "bearer" else None
        if project_id is None:
            raise HTTPException(status_code=401, detail="invalid task token")
        return project_id

    def _path(request: Request, name: str) -> Path:
        try:
            return state_file_path(state_dir, _project(request), name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/api/v1/current-task/state/{name}/data")
    async def get_state(request: Request, name: str) -> Response:
        path = _path(request, name)
        if not path.exists():
            raise HTTPException(status_code=404, detail="no such state file")
        return Response(path.read_bytes(), media_type="application/octet-stream")

    @router.put("/api/v1/current-task/state/{name}/data")
    async def put_state(request: Request, name: str) -> dict:
        path = _path(request, name)
        body = await request.body()
        if len(body) > MAX_STATE_SIZE:
            raise HTTPException(status_code=413, detail="state file too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {}

    return router
