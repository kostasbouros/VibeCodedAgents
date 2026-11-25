"""End-to-end tests for agents/device_watchdog/agent.py on the mock data (no network)."""

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from agents.device_watchdog import agent
from agents.device_watchdog.agent import CURSOR, Deps, build_items, run_watchdog
from agents.device_watchdog.schema import AlertExplanation, WatchdogResult
from shared.claude_client import ClaudeClient, Usage
from shared.cli import RunOptions
from shared.config import Settings
from shared.state import StateStore
from tests.device_watchdog.conftest import NOW, FakeMobility


class SpyClaude:
    """Answers every item it receives; keeps the text it was sent."""

    def __init__(self, escalate: bool = False) -> None:
        self.users: list[str] = []
        self.escalate = escalate

    def ask(self, **kw: Any) -> tuple[WatchdogResult, Usage]:
        self.users.append(kw["user"])
        body = kw["user"].split("<alerts>")[1].split("</alerts>")[0]
        items = [AlertExplanation(alert_id=i["alert_id"], explanation_en="Explained.",
                                  suggested_step="getstatus", notice_el="Ειδοποίηση.",
                                  notice_en="Notice.", escalate=self.escalate)
                 for i in json.loads(body)]  # fmt: skip
        return WatchdogResult(items=items), Usage(model="test")


@pytest.fixture(autouse=True)
def _traces_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.trace.TRACES_DIR", tmp_path / "traces")


def _options(dry_run: bool = False, limit: int | None = None) -> RunOptions:
    return RunOptions("device_watchdog", True, dry_run, limit, False, Settings())


def _deps(tmp_path: Path, store: StateStore, mobility: FakeMobility, claude: Any) -> Deps:
    deps = Deps(mobility=mobility, claude=claude, store=store, now=NOW,
                reports_dir=tmp_path / "reports", cache_path=tmp_path / "cache.json")  # fmt: skip
    deps.notices: list = []  # type: ignore[attr-defined]
    deps.notify = lambda *a, **kw: deps.notices.append((a, kw))  # type: ignore[attr-defined]
    deps.printed: list = []  # type: ignore[attr-defined]
    deps.echo = deps.printed.append  # type: ignore[attr-defined]
    return deps


def test_full_run_writes_report_and_marks_notified(tmp_path, store, mobility) -> None:
    deps = _deps(tmp_path, store, mobility, SpyClaude())
    path = run_watchdog(_options(), deps)
    assert path is not None and path.name == "health_2026-11-25_1200.md"
    text = path.read_text(encoding="utf-8")
    for part in ("## Summary", "## New alerts", "## Groups", "Draft notice (EL)", "getstatus"):
        assert part in text
    rows = list(csv.DictReader(path.with_suffix(".csv").open(encoding="utf-8-sig")))
    assert {r["rule_id"] for r in rows} == {"OFFLINE_01", "POWER_01", "JAM_01"}
    assert all(a["state"] == "notified" for a in store.open_alerts("device_watchdog"))
    ((_args, kw),) = deps.notices
    assert kw["counts"]["new_alerts"] == len(rows) and kw["path"] == str(path)
    assert store.get_cursor(CURSOR) is not None
    assert mobility.calls == ["list_units", "latest_positions", "alerts"]  # GET calls only


def test_second_run_finishes_quietly(tmp_path, store, mobility) -> None:
    claude = SpyClaude()
    run_watchdog(_options(), _deps(tmp_path, store, mobility, claude))
    deps = _deps(tmp_path, store, mobility, claude)
    assert run_watchdog(_options(), deps) is None
    assert len(claude.users) == 1 and deps.notices == []
    assert "no new alerts" in deps.printed[-1]


def test_dry_run_prints_and_changes_nothing(tmp_path, store, mobility) -> None:
    deps = _deps(tmp_path, store, mobility, SpyClaude())
    assert run_watchdog(_options(dry_run=True), deps) is None
    assert "# Device health report" in deps.printed[0]
    assert store.open_alerts() == [] and store.get_cursor(CURSOR) is None
    assert not (tmp_path / "reports").exists() and not (tmp_path / "cache.json").exists()
    assert deps.notices == []


def test_claude_sees_tokens_not_names_or_ids(tmp_path, store, mobility) -> None:
    claude = SpyClaude()
    run_watchdog(_options(), _deps(tmp_path, store, mobility, claude))
    sent = "".join(claude.users)
    assert "UNIT_01" in sent and "<alerts>" in sent
    for p in mobility.positions:
        assert p.name not in sent and p.uid not in sent


def test_rules_decide_escalation_not_claude(tmp_path, store, mobility) -> None:
    deps = _deps(tmp_path, store, mobility, SpyClaude(escalate=True))  # Claude says yes to all
    path = run_watchdog(_options(), deps)
    rows = list(csv.DictReader(path.with_suffix(".csv").open(encoding="utf-8-sig")))
    escalated = {r["rule_id"] for r in rows if r["escalate"] == "True"}
    assert escalated == {"POWER_01", "JAM_01"}


def test_batch_size_is_twenty() -> None:
    assert agent.BATCH_SIZE == 20


def test_items_are_sent_in_batches(tmp_path, store, mobility, monkeypatch) -> None:
    monkeypatch.setattr(agent, "BATCH_SIZE", 4)
    claude = SpyClaude()
    run_watchdog(_options(), _deps(tmp_path, store, mobility, claude))
    batches = [json.loads(u.split("<alerts>")[1].split("</alerts>")[0]) for u in claude.users]
    ids = [i["alert_id"] for b in batches for i in b]
    assert len(batches) >= 2 and all(len(b) <= 4 for b in batches)
    assert len(ids) == len(set(ids))


def test_limit_reports_at_most_n_and_keeps_rest_for_later(tmp_path, store, mobility) -> None:
    path = run_watchdog(_options(limit=2), _deps(tmp_path, store, mobility, SpyClaude()))
    rows = list(csv.DictReader(path.with_suffix(".csv").open(encoding="utf-8-sig")))
    assert len(rows) == 2
    states = [a["state"] for a in store.open_alerts("device_watchdog")]
    assert states.count("notified") == 2 and states.count("new") == 7


def test_mock_claude_fixture_matches_schema(tmp_path, store, mobility) -> None:
    deps = _deps(tmp_path, store, mobility, ClaudeClient(mock=True, settings=Settings()))
    path = run_watchdog(_options(), deps)
    assert "Mock explanation" in path.read_text(encoding="utf-8")


def test_group_is_sent_as_one_item(tmp_path, store, mobility) -> None:
    from agents.device_watchdog.alerts import AlertGroup, AlertView
    from agents.device_watchdog.rules import RuleHit

    def view(i: int) -> AlertView:
        hit = RuleHit(rule_id="OFFLINE_01", rule_version=1, unit_uid=f"U{i}",
                      severity="warning", escalate=False, summary="s", evidence={})  # fmt: skip
        return AlertView(f"U{i}:OFFLINE_01", f"U{i}", "OFFLINE_01", "new", NOW, hit)

    views = [view(i) for i in range(6)]
    group = AlertGroup("OFFLINE_01:carrier:30697", "probable carrier/network issue",
                       "OFFLINE_01", [v.alert_id for v in views[:5]], 5, 3)  # fmt: skip
    items, refs, _ = build_items(views, [group], {})
    assert [i["alert_id"] for i in items] == ["G01", "A01"]
    assert refs == {"G01": group.group_id, "A01": "U5:OFFLINE_01"}
    assert len(items[0]["members"]) == 5
