from __future__ import annotations

from fastapi import APIRouter

from air_llm.api.v1 import admin, inference, system

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(inference.router)
api_router.include_router(admin.router)
