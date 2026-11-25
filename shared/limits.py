"""Technical limits from config/limits.yaml, checked when first loaded."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, NonNegativeFloat, PositiveFloat, PositiveInt

LIMITS_FILE = Path(__file__).resolve().parents[1] / "config" / "limits.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # a typo'd name is an error


class HttpLimits(_Section):
    max_attempts: PositiveInt
    base_wait_seconds: PositiveFloat
    max_jitter_seconds: NonNegativeFloat
    max_retry_after_seconds: PositiveFloat


class ClaudeLimits(_Section):
    max_tokens: PositiveInt
    max_validation_error_chars: PositiveInt


class StateLimits(_Section):
    claim_stale_after_minutes: PositiveFloat
    max_error_chars: PositiveInt


class TraceLimits(_Section):
    max_note_chars: PositiveInt


class RedactLimits(_Section):
    signoff_search_lines: PositiveInt


class Limits(_Section):
    http: HttpLimits
    claude: ClaudeLimits
    state: StateLimits
    trace: TraceLimits
    redact: RedactLimits


def load_limits(path: Path = LIMITS_FILE) -> Limits:
    with path.open(encoding="utf-8") as f:
        return Limits.model_validate(yaml.safe_load(f))


@lru_cache
def limits() -> Limits:
    """The project's limits, loaded once."""
    return load_limits()
