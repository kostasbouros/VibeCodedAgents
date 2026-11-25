"""Device watchdog run: Mobility data -> rules -> alerts -> Claude explanations -> report.

    python -m agents.device_watchdog --mock --dry-run

1. Read units, latest positions (since cursor "watchdog.last_received") and alerts from Mobility.
   GET endpoints only: MobilityClient is read-only by design.
2. Build snapshots (merged with the cache of earlier runs) and evaluate config/rules.yaml.
3. Update alerts (new / open / resolved) and group related new alerts.
4. No new alerts: finish quietly. Otherwise send them (tokens only, no names or IDs) in
   batches to Claude with prompts/device_watchdog/v1.md and validate the replies.
5. Write reports/watchdog/health_<YYYY-MM-DD_HHMM>.md and .csv, mark the alerts notified and
   notify staff. --dry-run prints the report and changes nothing (no state, file or notice).

Claude only explains and drafts. Rules decide what is a problem and what is escalated; Claude's
own "escalate" is checked against the rules and the rules win. Nothing is sent to customers or
devices: suggested steps and notices are proposals for staff.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import typer

from agents.device_watchdog import alerts as al
from agents.device_watchdog.report import Report, render_csv, render_markdown
from agents.device_watchdog.rules import evaluate_all, history_needed, load_rules
from agents.device_watchdog.schema import AlertExplanation, WatchdogResult
from agents.device_watchdog.snapshot import (
    CACHE_FILE,
    UnitSnapshot,
    build_snapshots,
    load_cache,
    load_field_map,
    merge_snapshots,
    save_cache,
    with_api_alerts,
)
from connectors.mobility import Alert, Unit, UnitLatestPosition
from shared.claude_client import ClaudeClient
from shared.cli import RunOptions
from shared.notify import notify
from shared.redact import redact_or_raise
from shared.state import PROJECT_ROOT, StateStore, default_store
from shared.trace import Trace

AGENT = al.AGENT
AGENT_VERSION = "0.1.0"
PROMPT_VERSION = "v1"
PROMPT_FILE = PROJECT_ROOT / "prompts" / "device_watchdog" / f"{PROMPT_VERSION}.md"
REPORTS_DIR = PROJECT_ROOT / "reports" / "watchdog"
CURSOR = "watchdog.last_received"
BATCH_SIZE = 20  # items (alerts or groups) per Claude call
GROUP_SAMPLE = 10  # members of a group shown to Claude; the report lists them all


class MobilityReader(Protocol):
    """The GET calls the watchdog uses (MobilityClient, or a fake in tests)."""

    def list_units(self) -> list[Unit]: ...
    def latest_positions(self, since: datetime | None = None) -> list[UnitLatestPosition]: ...
    def alerts(self) -> tuple[list[Alert], str | None]: ...


class Asker(Protocol):
    def ask(self, **kwargs: Any) -> tuple[WatchdogResult, Any]: ...


@dataclass
class Deps:
    mobility: MobilityReader
    claude: Asker
    store: StateStore
    now: datetime
    reports_dir: Path = REPORTS_DIR
    cache_path: Path = CACHE_FILE
    notify: Callable[..., None] = notify
    echo: Callable[[str], None] = typer.echo


# --- Steps ------------------------------------------------------------------------------------


def _collect(
    deps: Deps, trace: Trace, history_len: int
) -> tuple[dict[str, UnitSnapshot], datetime | None]:
    """Snapshots of every known unit, and the newest report time seen in this fetch."""
    cursor = deps.store.get_cursor(CURSOR)
    since = datetime.fromisoformat(cursor) if cursor else None
    with trace.tool_call("mobility.list_units"):
        units = deps.mobility.list_units()
    with trace.tool_call("mobility.latest_positions"):
        positions = deps.mobility.latest_positions(since=since)
    with trace.tool_call("mobility.alerts"):
        api_alerts, _ = deps.mobility.alerts()
    fresh = build_snapshots(positions, units, load_field_map())
    merged = merge_snapshots(load_cache(deps.cache_path), fresh, history_len)
    newest = max((s.last_report_utc for s in fresh if s.last_report_utc), default=None)
    return with_api_alerts(merged, api_alerts), newest


def _alert_item(view: al.AlertView, token: str, snap: UnitSnapshot | None) -> dict[str, Any]:
    sleep = snap.value("sleep_mode") if snap else None
    return {
        "unit_token": token, "unit_type": view.unit_type, "rule_id": view.rule_id,
        "severity": view.hit.severity, "finding": view.hit.summary,
        "evidence": view.hit.evidence, "sleep_mode": "unknown" if sleep is None else sleep,
    }  # fmt: skip


def build_items(
    views: Sequence[al.AlertView], groups: Sequence[al.AlertGroup], snaps: dict[str, UnitSnapshot]
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, bool]]:
    """(items for Claude, reference -> alert/group id, reference -> rules' escalate).
    Claude sees references (A01, G01) and unit tokens (UNIT_01) only."""
    by_id = {v.alert_id: v for v in views}
    tokens = {v.unit_uid: f"UNIT_{i:02d}" for i, v in enumerate(views, 1)}
    items: list[dict[str, Any]] = []
    refs: dict[str, str] = {}
    escalate: dict[str, bool] = {}
    for n, g in enumerate(groups, 1):
        members = [by_id[a] for a in g.alert_ids if a in by_id]
        ref = f"G{n:02d}"
        refs[ref], escalate[ref] = g.group_id, any(m.hit.escalate for m in members)
        items.append({"alert_id": ref, "group_label": g.label, "rule_id": g.rule_id,
                      "unit_count": g.unit_count, "customer_count": g.company_count,
                      "members": [_alert_item(m, tokens[m.unit_uid], snaps.get(m.unit_uid))
                                  for m in members[:GROUP_SAMPLE]]})  # fmt: skip
    grouped = {a for g in groups for a in g.alert_ids}
    singles = [v for v in views if v.alert_id not in grouped]
    for n, v in enumerate(singles, 1):
        ref = f"A{n:02d}"
        refs[ref], escalate[ref] = v.alert_id, v.hit.escalate
        items.append({"alert_id": ref} | _alert_item(v, tokens[v.unit_uid], snaps.get(v.unit_uid)))
    return items, refs, escalate


def _ask(deps: Deps, trace: Trace, batch: list[dict[str, Any]]) -> WatchdogResult:
    data = json.dumps(batch, ensure_ascii=False, indent=1, default=str)
    user = redact_or_raise(f"<alerts>\n{data}\n</alerts>")  # raises (and stops) if unsure
    result, usage = deps.claude.ask(
        system=PROMPT_FILE.read_text(encoding="utf-8"), user=user, schema=WatchdogResult,
        agent=AGENT, prompt_version=PROMPT_VERSION,
    )  # fmt: skip
    trace.record_claude(usage)
    return result


def explain(
    deps: Deps, trace: Trace, items: list[dict[str, Any]], refs: dict[str, str],
    escalate: dict[str, bool],
) -> tuple[dict[str, AlertExplanation], list[str]]:  # fmt: skip
    """(explanations by alert/group id, ids with no explanation). The rules' escalate wins."""
    found: dict[str, AlertExplanation] = {}
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        sent = {i["alert_id"] for i in batch}
        result = _ask(deps, trace, batch)
        got = {it.alert_id: it for it in result.items if it.alert_id in sent}
        unknown = len({it.alert_id for it in result.items} - sent)
        trace.record_validation(
            "watchdog_ids",
            passed=not unknown and got.keys() == sent,
            detail=f"sent {len(sent)}, matched {len(got)}, unknown {unknown}",
        )
        clashes = sum(1 for ref, it in got.items() if it.escalate != escalate[ref])
        trace.record_validation("watchdog_escalate_matches_rules", passed=clashes == 0,
                                detail=f"{clashes} differ; rules kept")  # fmt: skip
        for ref, it in got.items():
            found[refs[ref]] = it.model_copy(
                update={"alert_id": refs[ref], "escalate": escalate[ref]}
            )
    missing = [refs[r] for r in refs if refs[r] not in found]
    return found, missing


def _save_report(deps: Deps, report: Report) -> Path:
    deps.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = deps.reports_dir / f"health_{deps.now:%Y-%m-%d_%H%M}"
    md, csv_path = stem.with_suffix(".md"), stem.with_suffix(".csv")
    md.write_text(render_markdown(report), encoding="utf-8")
    csv_path.write_text(render_csv(report), encoding="utf-8-sig")  # opens cleanly in Excel
    return md


# --- Run --------------------------------------------------------------------------------------


def run_watchdog(options: RunOptions, deps: Deps) -> Path | None:
    """One watchdog run. Returns the report path (None when nothing new or --dry-run)."""
    with Trace(AGENT, agent_version=AGENT_VERSION, prompt_version=PROMPT_VERSION,
               dry_run=options.dry_run, mock=options.mock, store=deps.store) as trace:  # fmt: skip
        rules = load_rules()
        snaps, newest = _collect(deps, trace, history_needed(rules))
        hits = evaluate_all(rules, snaps.values(), deps.now)
        plan = al.plan_alerts(
            hits,
            snaps,
            deps.store.open_alerts(AGENT),
            lambda aid: deps.store.get_alert(aid) is not None,
            deps.now,
        )
        if not options.dry_run:
            al.apply_plan(deps.store, plan)
            save_cache(snaps, deps.cache_path)
            if newest is not None:
                deps.store.set_cursor(CURSOR, newest.isoformat())
        views = plan.unnotified()[: options.limit]
        known = [s for s in plan.seen if s.state not in al.UNNOTIFIED]
        groups = al.group_new_alerts(views + known, deps.now, al.load_grouping())
        if not views:
            trace.set_outcome("no new alerts", items=0)
            deps.echo(f"{AGENT}: {len(snaps)} units checked, no new alerts.")
            return None
        for v in views:
            trace.add_input("alert", v.alert_id)
        items, refs, escalate = build_items(views, groups, snaps)
        explanations, missing = explain(deps, trace, items, refs, escalate)
        report = Report(deps.now, len(snaps), len(hits), len(plan.resolve), views, groups,
                        explanations, missing)  # fmt: skip
        return _finish(options, deps, trace, report)


def _finish(options: RunOptions, deps: Deps, trace: Trace, report: Report) -> Path | None:
    counts = {"units": report.units_checked, "new_alerts": len(report.alerts),
              "groups": len(report.groups), "escalate": report.escalations,
              "resolved": report.resolved}  # fmt: skip
    trace.set_outcome(f"{len(report.alerts)} new alerts", items=len(report.alerts))
    if options.dry_run:
        deps.echo(render_markdown(report))
        deps.echo(f"\n{AGENT}: dry run - nothing saved, marked or notified.")
        return None
    path = _save_report(deps, report)
    al.mark_notified(deps.store, report.alerts)
    deps.notify(AGENT, f"{len(report.alerts)} new device alerts", counts=counts, path=str(path))
    deps.echo(f"{AGENT}: report written to {path}")
    return path


def run(options: RunOptions) -> None:
    from connectors.mobility import get_mobility_client  # read-only client

    # Real clock in mock mode too: the mock server shifts its data so it looks current.
    with get_mobility_client() as mobility:
        deps = Deps(mobility=mobility, claude=ClaudeClient(mock=options.mock),
                    store=default_store(), now=datetime.now(UTC),
                    cache_path=CACHE_FILE)  # fmt: skip
        run_watchdog(options, deps)
