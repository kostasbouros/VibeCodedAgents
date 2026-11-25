"""Entry point: python -m agents.device_watchdog --mock --dry-run"""

from agents.device_watchdog.agent import run
from shared.cli import build_app

app = build_app("device_watchdog", run)

if __name__ == "__main__":
    app()
