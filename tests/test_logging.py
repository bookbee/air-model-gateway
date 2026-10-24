from __future__ import annotations

import json

import pytest

from air_model_gateway.config import LogSettings, Settings
from air_model_gateway.observability.logging import configure_logging, get_logger


def test_json_logs_carry_a_cloud_logging_severity(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(log=LogSettings(render="json")))
    get_logger("t").error("model.call.failed", provider="gemini")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["severity"] == "ERROR"
    assert line["level"] == "error"
    assert line["event"] == "model.call.failed"
