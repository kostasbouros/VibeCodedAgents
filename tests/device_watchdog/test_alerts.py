"""Tests for agents/device_watchdog/alerts.py: lifecycle, no duplicates, grouping."""

from datetime import timedelta

from agents.device_watchdog.alerts import (
    CARRIER_LABEL,
    SITE_LABEL,
    AlertView,
    GroupingConfig,
    group_new_alerts,
    load_grouping,
    mark_notified,
    plan_alerts,
    sync_alerts,
)
from agents.device_watchdog.rules import RuleHit
from shared.state import StateStore
from tests.device_watchdog.conftest import NOW, snap

CFG = GroupingConfig(min_units=5, window_minutes=15, carrier_min_companies=2)


def _hit(uid: str = "U1", rule_id: str = "GPS_01") -> RuleHit:
    return RuleHit(rule_id=rule_id, rule_version=1, unit_uid=uid, severity="warning",
                   escalate=False, summary="test", evidence={"satellites": 1})  # fmt: skip


def _state(store: StateStore, aid: str) -> str:
    alert = store.get_alert(aid)
    assert alert is not None
    return alert["state"]


# --- Lifecycle ------------------------------------------------------------------------------


def test_new_then_open_then_resolved(store: StateStore) -> None:
    snaps = {"U1": snap("U1")}
    plan = sync_alerts(store, [_hit()], snaps, NOW)
    assert [v.alert_id for v in plan.new] == ["U1:GPS_01"]
    assert _state(store, "U1:GPS_01") == "new"

    plan = sync_alerts(store, [_hit()], snaps, NOW)  # still hitting
    assert plan.new == [] and _state(store, "U1:GPS_01") == "open"

    plan = sync_alerts(store, [], snaps, NOW)  # rule no longer hits
    assert plan.resolve == ["U1:GPS_01"] and _state(store, "U1:GPS_01") == "resolved"


def test_rerun_creates_no_duplicates(store: StateStore) -> None:
    snaps = {"U1": snap("U1"), "U2": snap("U2")}
    hits = [_hit("U1"), _hit("U2"), _hit("U1", "POWER_01")]
    sync_alerts(store, hits, snaps, NOW)
    for _ in range(3):
        plan = sync_alerts(store, hits, snaps, NOW)
        assert plan.new == []
    assert len(store.open_alerts("device_watchdog")) == 3


def test_last_seen_is_refreshed(store: StateStore) -> None:
    sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    first = store.get_alert("U1:GPS_01")
    sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    second = store.get_alert("U1:GPS_01")
    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] >= first["last_seen"]


def test_notified_alert_is_not_reported_again(store: StateStore) -> None:
    plan = sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    mark_notified(store, plan.new)
    plan = sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    assert plan.unnotified() == [] and _state(store, "U1:GPS_01") == "notified"


def test_unnotified_leftovers_are_reported_next_run(store: StateStore) -> None:
    sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)  # run failed before notifying
    plan = sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    assert [v.alert_id for v in plan.unnotified()] == ["U1:GPS_01"]


def test_problem_coming_back_gets_a_new_alert(store: StateStore) -> None:
    snaps = {"U1": snap("U1")}
    sync_alerts(store, [_hit()], snaps, NOW)
    sync_alerts(store, [], snaps, NOW)
    plan = sync_alerts(store, [_hit()], snaps, NOW)
    assert [v.alert_id for v in plan.new] == ["U1:GPS_01:2"]


def test_unit_missing_from_data_is_not_resolved(store: StateStore) -> None:
    sync_alerts(store, [_hit()], {"U1": snap("U1")}, NOW)
    plan = sync_alerts(store, [], {"U2": snap("U2")}, NOW)
    assert plan.resolve == [] and _state(store, "U1:GPS_01") == "new"


def test_plan_writes_nothing(store: StateStore) -> None:
    plan_alerts([_hit()], {"U1": snap("U1")}, store.open_alerts(), lambda _: False, NOW)
    assert store.open_alerts() == []


# --- Grouping -------------------------------------------------------------------------------


def _view(uid: str, *, carrier: str | None, company: str, rule: str = "OFFLINE_01",
          minutes_ago: float = 0, state: str = "new") -> AlertView:  # fmt: skip
    return AlertView(alert_id=f"{uid}:{rule}", unit_uid=uid, rule_id=rule, state=state,
                     first_seen=NOW - timedelta(minutes=minutes_ago), hit=_hit(uid, rule),
                     carrier_hint=carrier, company_uid=company)  # fmt: skip


def test_grouping_config_loads_from_thresholds() -> None:
    assert load_grouping() == CFG


def test_carrier_outage_group() -> None:
    views = [_view(f"U{i}", carrier="30697", company=f"C{i % 3}", minutes_ago=i * 2)
             for i in range(6)]  # fmt: skip
    groups = group_new_alerts(views, NOW, CFG)
    assert len(groups) == 1
    g = groups[0]
    assert g.label == CARRIER_LABEL and g.unit_count == 6 and g.company_count == 3
    assert g.group_id == "OFFLINE_01:carrier:30697"


def test_customer_site_group() -> None:
    views = [_view(f"U{i}", carrier=f"3069{i}", company="C1") for i in range(5)]
    groups = group_new_alerts(views, NOW, CFG)
    assert [(g.label, g.unit_count) for g in groups] == [(SITE_LABEL, 5)]


def test_same_carrier_single_customer_is_a_site_issue() -> None:
    views = [_view(f"U{i}", carrier="30697", company="C1") for i in range(5)]
    assert [g.label for g in group_new_alerts(views, NOW, CFG)] == [SITE_LABEL]


def test_no_group_below_min_units() -> None:
    views = [_view(f"U{i}", carrier="30697", company=f"C{i}") for i in range(4)]
    assert group_new_alerts(views, NOW, CFG) == []


def test_alerts_outside_window_are_not_grouped() -> None:
    views = [_view(f"U{i}", carrier="30697", company=f"C{i}", minutes_ago=20 if i else 0)
             for i in range(6)]  # fmt: skip
    assert group_new_alerts(views, NOW, CFG) == []


def test_different_rules_are_not_grouped() -> None:
    views = [_view(f"U{i}", carrier="30697", company=f"C{i}",
                   rule="OFFLINE_01" if i % 2 else "GSM_01") for i in range(6)]  # fmt: skip
    assert group_new_alerts(views, NOW, CFG) == []


def test_group_of_already_notified_alerts_is_not_returned() -> None:
    views = [_view(f"U{i}", carrier="30697", company=f"C{i}", state="notified") for i in range(5)]
    assert group_new_alerts(views, NOW, CFG) == []
