from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from air_model_gateway.config import PolicySettings, SecuritySettings, Settings
from air_model_gateway.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        security=SecuritySettings(
            service_tokens={"t-sent": "air-classifier-service", "t-client": "air-web-console"}
        ),
        policy=PolicySettings(config_path="deploy/policy/policy.local.yaml"),
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
