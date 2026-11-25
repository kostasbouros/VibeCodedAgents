"""The watchdog's health report: Markdown for people, CSV for spreadsheets.

Customer notices in the report are drafts for staff review. The agent never sends them.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from agents.device_watchdog.alerts import AlertGroup, AlertView
from agents.device_watchdog.schema import AlertExplanation


@dataclass(frozen=True)
class Report:
    now: datetime
    units_checked: int
    hits: int
    resolved: int
    alerts: list[AlertView]  # new (not yet notified) alerts
    groups: list[AlertGroup]
    explanations: dict[str, AlertExplanation]  # by alert_id or group_id
    missing: list[str] = field(default_factory=list)  # ids Claude gave no explanation for

    def group_of(self, alert_id: str) -> AlertGroup | None:
        return next((g for g in self.groups if alert_id in g.alert_ids), None)

    def explanation_for(self, alert_id: str) -> AlertExplanation | None:
        group = self.group_of(alert_id)
        return self.explanations.get(group.group_id if group else alert_id)

    @property
    def escalations(self) -> int:
        return sum(1 for a in self.alerts if a.hit.escalate)


def _cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _summary(r: Report) -> list[str]:
    return [
        f"# Device health report - {r.now:%Y-%m-%d %H:%M} UTC", "",
        "## Summary", "",
        f"- Units checked: {r.units_checked}",
        f"- Rule hits: {r.hits}",
        f"- New alerts: {len(r.alerts)} (escalate: {r.escalations})",
        f"- Groups: {len(r.groups)}",
        f"- Resolved since last run: {r.resolved}",
        *([f"- No explanation returned for: {', '.join(r.missing)}"] if r.missing else []),
        "",
    ]  # fmt: skip


def _alert_table(r: Report) -> list[str]:
    lines = ["## New alerts", "",
             "| Alert | Unit | Type | Rule | Severity | Escalate | Finding | Group | Suggested step |",
             "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for a in r.alerts:
        group, exp = r.group_of(a.alert_id), r.explanation_for(a.alert_id)
        lines.append("| " + " | ".join(_cell(v) for v in (
            a.alert_id, a.unit_name, a.unit_type, a.rule_id, a.hit.severity,
            "YES" if a.hit.escalate else "no", a.hit.summary,
            group.label if group else "", exp.suggested_step if exp else "",
        )) + " |")  # fmt: skip
    return lines + [""]


def _group_table(r: Report) -> list[str]:
    if not r.groups:
        return ["## Groups", "", "None.", ""]
    lines = [
        "## Groups",
        "",
        "| Group | Label | Rule | Units | Customers |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {_cell(g.group_id)} | {g.label} | {g.rule_id} | {g.unit_count} | "
              f"{g.company_count} |" for g in r.groups]  # fmt: skip
    return lines + [""]


def _explanations(r: Report) -> list[str]:
    lines = [
        "## Explanations and draft notices",
        "",
        "_Drafts for staff review. Nothing has been sent to customers or devices._",
        "",
    ]
    for key, exp in r.explanations.items():
        lines += [f"### {key}", "", exp.explanation_en, "",
                  f"- Suggested step (needs approval): **{exp.suggested_step}**",
                  f"- Draft notice (EN): {exp.notice_en}",
                  f"- Draft notice (EL): {exp.notice_el}", ""]  # fmt: skip
    return lines


def render_markdown(r: Report) -> str:
    return "\n".join(_summary(r) + _alert_table(r) + _group_table(r) + _explanations(r))


CSV_COLUMNS = [
    "alert_id", "unit_uid", "unit_name", "unit_type", "rule_id", "severity", "escalate",
    "finding", "group_id", "group_label", "explanation_en", "suggested_step",
    "notice_en", "notice_el",
]  # fmt: skip


def render_csv(r: Report) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for a in r.alerts:
        group, exp = r.group_of(a.alert_id), r.explanation_for(a.alert_id)
        writer.writerow({
            "alert_id": a.alert_id, "unit_uid": a.unit_uid, "unit_name": a.unit_name,
            "unit_type": a.unit_type, "rule_id": a.rule_id, "severity": a.hit.severity,
            "escalate": a.hit.escalate, "finding": a.hit.summary,
            "group_id": group.group_id if group else "", "group_label": group.label if group else "",
            "explanation_en": exp.explanation_en if exp else "",
            "suggested_step": exp.suggested_step if exp else "",
            "notice_en": exp.notice_en if exp else "", "notice_el": exp.notice_el if exp else "",
        })  # fmt: skip
    return out.getvalue()
