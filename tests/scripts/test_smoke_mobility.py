"""Tests for scripts/smoke_mobility.py against the in-process mock (no network)."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from connectors.mobility import MobilityClient
from mock_api.server import app

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_mobility.py"
_spec = importlib.util.spec_from_file_location("smoke_mobility", SCRIPT)
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def mock_client() -> MobilityClient:
    client = MobilityClient("http://testserver", "mock", "mock")
    client._http = TestClient(app)  # same requests, answered by the mock in this process
    return client


def test_prints_only_the_count_and_three_units() -> None:
    lines = smoke.run(mock_client())
    assert lines[0] == "Units: 20"
    assert len(lines) == 1 + smoke.SAMPLE_UNITS
    for line, name in zip(lines[1:], ("VEH_001", "VEH_002", "VEH_003"), strict=True):
        parts = line.split(" | ")
        assert parts[0] == name
        assert parts[2] in {"on", "off"}
        assert parts[3].endswith("sensor readings")


def test_nothing_personal_is_printed() -> None:
    text = "\n".join(smoke.run(mock_client()))
    for word in ("Latitude", "Address", "+30", "GR ["):
        assert word not in text
