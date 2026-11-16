"""API routes for identity, workspaces, transcription and review."""

from fastapi import APIRouter

from app.api.v1.routes import (
    cleanup,
    jobs,
    meetings,
    meta,
    notes,
    notifications,
    speakers,
    study,
    users,
    workspaces,
)

api_router = APIRouter()
for module in (
    meta,
    users,
    workspaces,
    meetings,
    notes,
    cleanup,
    study,
    speakers,
    notifications,
    jobs,
):
    api_router.include_router(module.router)
api_router.include_router(speakers.rights_router)
