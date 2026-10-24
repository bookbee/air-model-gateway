from __future__ import annotations

from fastapi import APIRouter

from air_model_gateway.api.v1 import admin, embed, generate, system

api_router = APIRouter()
api_router.include_router(generate.router)
api_router.include_router(embed.router)
api_router.include_router(system.router)
api_router.include_router(admin.router)
