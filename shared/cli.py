"""Common command line used by every agent: python -m agents.<name> --mock --dry-run"""

import typer

from shared.config import get_settings


def build_app(agent_name: str) -> typer.Typer:
    """Create the command line for one agent."""
    app = typer.Typer(add_completion=False, help=f"Run the {agent_name} agent.")

    @app.command()
    def run(
        mock: bool = typer.Option(True, "--mock/--live", help="Use mock data (default)."),
        dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Write nothing (default)."),
    ) -> None:
        settings = get_settings()
        use_mocks = mock or settings.use_mocks
        typer.echo(
            f"{agent_name}: not built yet "
            f"(mocks={'on' if use_mocks else 'off'}, dry_run={'on' if dry_run else 'off'})"
        )

    return app
