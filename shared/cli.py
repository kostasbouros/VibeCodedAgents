"""Common command line for every agent.

    python -m agents.<name> [--mock | --live] [--dry-run] [--limit N] [--verbose]

--mock (default) uses mock data. --live refuses to start unless USE_MOCKS=false AND
ENVIRONMENT are explicitly set (in the environment or .env), so real systems are never
reached by accident. --dry-run prints what would happen and writes nothing external.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import structlog
import typer

from shared import config
from shared.config import Settings, get_settings


@dataclass(frozen=True)
class RunOptions:
    agent: str
    mock: bool
    dry_run: bool
    limit: int | None
    verbose: bool
    settings: Settings


AgentRun = Callable[[RunOptions], None]


class LiveModeRefused(Exception):
    """--live was asked for without the settings that allow it."""


def check_live_allowed(settings: Settings) -> None:
    """Raise LiveModeRefused unless USE_MOCKS=false and ENVIRONMENT were set on purpose."""
    problems = []
    if settings.use_mocks:
        problems.append("USE_MOCKS must be set to false")
    if "environment" not in settings.model_fields_set or not settings.environment.strip():
        problems.append("ENVIRONMENT must be set")
    if problems:
        raise LiveModeRefused("refusing --live: " + "; ".join(problems))


def configure_logging(settings: Settings, *, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))


def _not_built_yet(options: RunOptions) -> None:
    typer.echo(
        f"{options.agent}: not built yet (mocks={'on' if options.mock else 'off'}, "
        f"dry_run={'on' if options.dry_run else 'off'}, limit={options.limit})"
    )


def build_app(agent_name: str, run: AgentRun | None = None) -> typer.Typer:
    """Create the command line for one agent. `run` receives the parsed options."""
    app = typer.Typer(add_completion=False, help=f"Run the {agent_name} agent.")
    agent_run = run or _not_built_yet

    @app.command()
    def main(
        mock: bool = typer.Option(True, "--mock/--live", help="Mock data (default) or live."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Print only; write nothing."),
        limit: int | None = typer.Option(None, "--limit", min=1, help="Process at most N items."),
        verbose: bool = typer.Option(False, "--verbose", help="Show debug logs."),
    ) -> None:
        if mock:
            # --mock wins over USE_MOCKS=false in .env, for every part of the program:
            # anything that reads settings later (e.g. ClaudeClient) also sees mocks on.
            os.environ["USE_MOCKS"] = "true"
            config.get_settings.cache_clear()
        settings = get_settings()
        if not mock:
            try:
                check_live_allowed(settings)
            except LiveModeRefused as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from None
        configure_logging(settings, verbose=verbose)
        agent_run(RunOptions(agent_name, mock, dry_run, limit, verbose, settings))

    return app
