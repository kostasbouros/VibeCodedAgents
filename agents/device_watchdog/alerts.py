"""Turn rule hits into alerts in the state store, and group related new alerts.

Lifecycle (shared/state.py, forward only): new -> open -> notified -> resolved.
- A hit with no unresolved alert for (unit, rule) creates one in state "new".
- A hit that already has an unresolved alert refreshes last_seen ("new" becomes "open").
- An unresolved alert whose rule no longer hits for that unit is resolved, but only if the
  unit was checked this run (a unit missing from the data is never "resolved" by accident).

Alert ids are f"{unit_uid}:{rule_id}". The store keeps a resolved alert resolved for good, so
if the same problem comes back later it gets f"{unit_uid}:{rule_id}:2", then ":3", and so on.

plan_alerts() decides without writing anything (used by --dry-run too); apply_plan() writes.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt

from agents.device_watchdog.rules import RuleHit
from agents.device_watchdog.snapshot import UnitSnapshot
from shared.state import PROJECT_ROOT, StateStore

AGENT = "device_watchdog"
THRESHOLDS_FILE = PROJECT_ROOT / "config" / "thresholds.yaml"
CARRIER_LABEL = "probable carrier/network issue"
SITE_LABEL = "probable customer-site issue"
UNNOTIFIED = ("new", "open")


class GroupingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    min_units: PositiveInt
    window_minutes: PositiveFloat
    carrier_min_companies: PositiveInt


def load_grouping(path: Path = THRESHOLDS_FILE) -> GroupingConfig:
    with path.open(encoding="utf-8") as f:
        return GroupingConfig.model_validate(yaml.safe_load(f)[AGENT]["grouping"])


def alert_id(unit_uid: str, rule_id: str, occurrence: int = 1) -> str:
    base = f"{unit_uid}:{rule_id}"
    return base if occurrence == 1 else f"{base}:{occurrence}"


# --- Planning (no writes) ---------------------------------------------------------------------


@dataclass(frozen=True)
class AlertView:
    """One alert as the report and grouping see it."""

    alert_id: str
    unit_uid: str
    rule_id: str
    state: str  # state after this run: new, open or notified
    first_seen: datetime
    hit: RuleHit
    unit_name: str | None = None
    unit_type: str | None = None
    carrier_hint: str | None = None
    company_uid: str | None = None


@dataclass(frozen=True)
class AlertPlan:
    new: list[AlertView] = field(default_factory=list)
    seen: list[AlertView] = field(default_factory=list)  # still hitting, already known
    resolve: list[str] = field(default_factory=list)  # alert ids

    def unnotified(self) -> list[AlertView]:
        """Alerts not reported yet: created now, or left over from a run that failed."""
        return self.new + [a for a in self.seen if a.state in UNNOTIFIED]


def _details(view: AlertView) -> dict[str, Any]:
    hit = view.hit
    return {
        "rule_version": hit.rule_version, "severity": hit.severity, "escalate": hit.escalate,
        "summary": hit.summary, "evidence": hit.evidence, "unit_type": view.unit_type,
        "carrier_hint": view.carrier_hint, "company_uid": view.company_uid,
    }  # fmt: skip


def _next_id(unit_uid: str, rule_id: str, exists: Callable[[str], bool]) -> str:
    n = 1
    while exists(alert_id(unit_uid, rule_id, n)):
        n += 1
    return alert_id(unit_uid, rule_id, n)


def _view(
    hit: RuleHit, snap: UnitSnapshot | None, aid: str, state: str, first: datetime
) -> AlertView:
    return AlertView(
        alert_id=aid, unit_uid=hit.unit_uid, rule_id=hit.rule_id, state=state,
        first_seen=first, hit=hit, unit_name=snap.name if snap else None,
        unit_type=snap.unit_type if snap else None,
        carrier_hint=snap.carrier_hint if snap else None,
        company_uid=snap.company_uid if snap else None,
    )  # fmt: skip


def plan_alerts(
    hits: Sequence[RuleHit],
    snapshots: dict[str, UnitSnapshot],
    open_alerts: Iterable[dict[str, Any]],
    exists: Callable[[str], bool],
    now: datetime,
) -> AlertPlan:
    """Decide what changes. `open_alerts`: StateStore.open_alerts(AGENT); `exists`: whether
    an alert id is already used (resolved ones included)."""
    current = {(a["unit_uid"], a["rule_id"]): a for a in open_alerts if a["agent"] == AGENT}
    plan = AlertPlan()
    for hit in hits:
        snap = snapshots.get(hit.unit_uid)
        known = current.get((hit.unit_uid, hit.rule_id))
        if known is None:
            aid = _next_id(hit.unit_uid, hit.rule_id, exists)
            plan.new.append(_view(hit, snap, aid, "new", now))
        else:
            state = "open" if known["state"] == "new" else known["state"]
            first = datetime.fromisoformat(known["first_seen"])
            plan.seen.append(_view(hit, snap, known["alert_id"], state, first))
    hit_keys = {(h.unit_uid, h.rule_id) for h in hits}
    plan.resolve.extend(
        a["alert_id"] for key, a in current.items()
        if key not in hit_keys and key[0] in snapshots
    )  # fmt: skip
    return plan


def apply_plan(store: StateStore, plan: AlertPlan) -> None:
    """Write the plan. Safe to repeat: upserts and resolves are idempotent."""
    for view in plan.new:
        store.upsert_alert(view.alert_id, agent=AGENT, rule_id=view.rule_id,
                           unit_uid=view.unit_uid, state="new", details=_details(view))  # fmt: skip
    for view in plan.seen:  # "open" never moves a notified alert backwards
        store.upsert_alert(view.alert_id, agent=AGENT, rule_id=view.rule_id,
                           unit_uid=view.unit_uid, state="open", details=_details(view))  # fmt: skip
    for aid in plan.resolve:
        store.resolve_alert(aid)


def mark_notified(store: StateStore, views: Iterable[AlertView]) -> None:
    for view in views:
        store.upsert_alert(view.alert_id, agent=AGENT, rule_id=view.rule_id,
                           unit_uid=view.unit_uid, state="notified")  # fmt: skip


def sync_alerts(
    store: StateStore, hits: Sequence[RuleHit], snapshots: dict[str, UnitSnapshot], now: datetime
) -> AlertPlan:
    """plan_alerts + apply_plan against a real store."""
    plan = plan_alerts(hits, snapshots, store.open_alerts(AGENT),
                       lambda aid: store.get_alert(aid) is not None, now)  # fmt: skip
    apply_plan(store, plan)
    return plan


# --- Grouping ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertGroup:
    group_id: str
    label: str
    rule_id: str
    alert_ids: list[str]
    unit_count: int
    company_count: int


def _groups_by(
    alerts: list[AlertView], key: Callable[[AlertView], str | None]
) -> dict[tuple[str, str], list[AlertView]]:
    groups: dict[tuple[str, str], list[AlertView]] = {}
    for a in alerts:
        if (k := key(a)) is not None:
            groups.setdefault((a.rule_id, k), []).append(a)
    return groups


def _make(kind: str, label: str, rule_id: str, value: str, members: list[AlertView]) -> AlertGroup:
    return AlertGroup(
        group_id=f"{rule_id}:{kind}:{value}", label=label, rule_id=rule_id,
        alert_ids=sorted(a.alert_id for a in members),
        unit_count=len({a.unit_uid for a in members}),
        company_count=len({a.company_uid for a in members if a.company_uid}),
    )  # fmt: skip


def group_new_alerts(
    alerts: Sequence[AlertView], now: datetime, cfg: GroupingConfig
) -> list[AlertGroup]:
    """Group alerts first seen within the window that share a rule and a cause.

    1. Same rule + same carrier_hint, min_units+ units across carrier_min_companies+
       customers -> "probable carrier/network issue".
    2. Of the rest, same rule + same company_uid, min_units+ units -> "probable customer-site
       issue".
    Only groups with at least one alert not yet notified are returned.
    """
    since = now - timedelta(minutes=cfg.window_minutes)
    recent = [a for a in alerts if a.first_seen >= since]
    found: list[AlertGroup] = []
    grouped: set[str] = set()
    for (rule_id, hint), members in _groups_by(recent, lambda a: a.carrier_hint).items():
        g = _make("carrier", CARRIER_LABEL, rule_id, hint, members)
        if g.unit_count >= cfg.min_units and g.company_count >= cfg.carrier_min_companies:
            found.append(g)
            grouped.update(g.alert_ids)
    rest = [a for a in recent if a.alert_id not in grouped]
    for (rule_id, company), members in _groups_by(rest, lambda a: a.company_uid).items():
        g = _make("company", SITE_LABEL, rule_id, company, members)
        if g.unit_count >= cfg.min_units:
            found.append(g)
    fresh = {a.alert_id for a in alerts if a.state in UNNOTIFIED}
    return [g for g in found if fresh & set(g.alert_ids)]
