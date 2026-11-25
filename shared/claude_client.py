"""The single way agents call Claude.

- Stable instructions first in the system prompt, marked for prompt caching.
- Claude replies with JSON only; the reply is validated with a Pydantic model. One retry
  with the validation error, then InvalidModelOutput.
- The user text is checked for personal data before anything is sent (RedactionError).
- Mock mode (USE_MOCKS=true or mock=True) reads tests/fixtures/claude/<agent>.json and never
  calls the API, so tests cost nothing.
- Logs agent, prompt version, model, tokens and estimated cost. Never prompt or reply text.
"""

import json
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import anthropic
import structlog
import yaml
from pydantic import BaseModel, ValidationError

from shared.config import Settings, get_settings
from shared.limits import limits
from shared.redact import RedactionError, find_personal_data

log = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "claude"
MODELS_FILE = PROJECT_ROOT / "config" / "models.yaml"
DEFAULT_MODEL = "claude-opus-5-5"  # only used when CLAUDE_MODEL is not set
_FALLBACK_BETA = "server-side-fallback-2026-07-01"  # re-runs refused requests on a fallback
_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_ERROR_CHARS = limits().claude.max_validation_error_chars
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

T = TypeVar("T", bound=BaseModel)


class InvalidModelOutput(Exception):
    """Claude's reply did not match the schema, even after one retry."""

    def __init__(self, message: str, raw_output: str, usage: "Usage | None" = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.usage = usage  # tokens already spent, so the trace can still count them


class ClaudeRefused(Exception):
    """Claude (and its fallback model) declined the request for safety reasons."""

    def __init__(self, message: str, usage: "Usage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage  # tokens already spent, so the trace can still count them


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    attempts: int = 0
    estimated_cost_usd: float | None = 0.0  # None when the model has no price in models.yaml


@lru_cache
def _prices() -> dict[str, dict[str, float]]:
    with MODELS_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["models"]


def estimate_cost(model: str, usage: Usage) -> float | None:
    """Estimated USD cost from config/models.yaml, or None if the model is not listed."""
    price = _prices().get(model)
    if price is None:
        return None
    total = (
        usage.input_tokens * price["input"]
        + usage.output_tokens * price["output"]
        + usage.cache_read_tokens * price["cache_read"]
        + usage.cache_write_tokens * price["cache_write"]
    )
    return round(total / 1_000_000, 6)


def _json_instructions(schema: type[BaseModel]) -> str:
    # sort_keys keeps the text byte-identical between calls, so the cache keeps hitting.
    schema_json = json.dumps(schema.model_json_schema(), sort_keys=True, indent=2)
    return (
        "Reply with one JSON object only: no explanation, no markdown, no code fences.\n"
        "It must validate against this JSON Schema:\n"
        f"<schema>\n{schema_json}\n</schema>"
    )


def _reply_text(response: Any) -> str:
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    fenced = _CODE_FENCE.match(text)
    return fenced.group(1) if fenced else text


class ClaudeClient:
    def __init__(
        self,
        *,
        mock: bool | None = None,
        settings: Settings | None = None,
        anthropic_client: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.mock = self._settings.use_mocks if mock is None else mock
        self._client = anthropic_client  # created on first real call if not given

    def ask(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        agent: str,
        prompt_version: str,
        model: str | None = None,
        max_tokens: int | None = None,  # None: limits.yaml claude.max_tokens
        temperature: float = 0.0,  # kept for callers; the API no longer accepts it
    ) -> tuple[T, Usage]:
        """Ask Claude and return (validated reply, usage).

        `temperature` is accepted but not sent: current Claude models reject sampling
        parameters, so determinism comes from the rules in code, not from Claude.
        """
        if not _AGENT_NAME.match(agent):
            raise ValueError(f"invalid agent name: {agent!r}")
        # Backstop only: names can't be detected here, so agents must still call
        # redact_or_raise() on customer text before building the prompt.
        for label, text in (("system", system), ("user", user)):
            found = find_personal_data(text)
            if found:
                raise RedactionError(
                    f"{label} text still contains personal data: {', '.join(found)}"
                )
        model = model or self._settings.claude_model or DEFAULT_MODEL
        max_tokens = max_tokens or limits().claude.max_tokens
        if self.mock:
            return self._ask_mock(schema, agent, model)
        return self._ask_live(system, user, schema, agent, prompt_version, model, max_tokens)

    def _ask_mock(self, schema: type[T], agent: str, model: str) -> tuple[T, Usage]:
        path = FIXTURES_DIR / f"{agent}.json"
        if not path.is_file():
            raise FileNotFoundError(f"mock mode: no fixture at {path}")
        raw = path.read_text(encoding="utf-8")
        try:
            return schema.model_validate_json(raw), Usage(model=model)
        except ValidationError as exc:
            raise InvalidModelOutput(f"fixture {path.name} does not match schema", raw) from exc

    def _ask_live(
        self,
        system: str,
        user: str,
        schema: type[T],
        agent: str,
        prompt_version: str,
        model: str,
        max_tokens: int,
    ) -> tuple[T, Usage]:
        system_blocks = [
            {"type": "text", "text": system},
            {
                "type": "text",
                "text": _json_instructions(schema),
                "cache_control": {"type": "ephemeral"},  # caches both stable blocks
            },
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        usage = Usage(model=model)
        for attempt in (1, 2):
            response = self._call(model, max_tokens, system_blocks, messages)
            usage = self._add_usage(usage, response)
            if response.stop_reason == "refusal":
                self._log(agent, prompt_version, usage, valid=False)
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise ClaudeRefused(f"Claude declined the request (category: {category})", usage)
            raw = _reply_text(response)
            try:
                result = schema.model_validate_json(raw)
            except ValidationError as exc:
                self._log(agent, prompt_version, usage, valid=False)
                if attempt == 2:
                    raise InvalidModelOutput(
                        "reply did not match schema after retry", raw, usage
                    ) from exc
                messages += [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": _retry_message(exc)},
                ]
                continue
            self._log(agent, prompt_version, usage, valid=True)
            return result, usage
        raise AssertionError("unreachable")

    def _call(self, model: str, max_tokens: int, system: list, messages: list) -> Any:
        response = self._anthropic().beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            betas=[_FALLBACK_BETA],
            fallbacks="default",
        )
        return response

    def _anthropic(self) -> Any:
        if self._client is None:
            key = self._settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set (or set USE_MOCKS=true)")
            self._client = anthropic.Anthropic(api_key=key.get_secret_value())
        return self._client

    @staticmethod
    def _add_usage(usage: Usage, response: Any) -> Usage:
        u = response.usage
        served_by = getattr(response, "model", None) or usage.model
        total = replace(
            usage,
            model=served_by,
            input_tokens=usage.input_tokens + (u.input_tokens or 0),
            output_tokens=usage.output_tokens + (u.output_tokens or 0),
            cache_read_tokens=usage.cache_read_tokens + (u.cache_read_input_tokens or 0),
            cache_write_tokens=usage.cache_write_tokens + (u.cache_creation_input_tokens or 0),
            attempts=usage.attempts + 1,
        )
        return replace(total, estimated_cost_usd=estimate_cost(served_by, total))

    @staticmethod
    def _log(agent: str, prompt_version: str, usage: Usage, *, valid: bool) -> None:
        log.info(
            "claude_call",
            agent=agent,
            prompt_version=prompt_version,
            model=usage.model,
            attempt=usage.attempts,
            valid=valid,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
        )


def _retry_message(exc: ValidationError) -> str:
    error = str(exc)[:_MAX_ERROR_CHARS]
    return (
        "Your previous reply did not match the required JSON Schema. Validation error:\n"
        f"<validation_error>\n{error}\n</validation_error>\n"
        "Reply again with only the corrected JSON object."
    )
