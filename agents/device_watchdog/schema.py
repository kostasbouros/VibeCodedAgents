"""What Claude must return for the device watchdog (validated before anything uses it)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SuggestedStep = Literal[
    "getstatus",
    "getgps",
    "web_connect",
    "config push",
    "reboot",
    "schedule site visit",
    "contact customer",
    "none",
]


class AlertExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(min_length=1, max_length=20)  # the reference we sent (A01, G01...)
    explanation_en: str = Field(min_length=1, max_length=600)
    suggested_step: SuggestedStep  # a proposal for a human to approve; never executed
    notice_el: str = Field(min_length=1, max_length=600)  # draft only; never sent by the agent
    notice_en: str = Field(min_length=1, max_length=600)
    escalate: bool  # compared with the rules' decision; the rules win


class WatchdogResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AlertExplanation]
