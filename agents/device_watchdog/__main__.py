"""Entry point: python -m agents.device_watchdog --mock --dry-run"""

from shared.cli import build_app

app = build_app("device_watchdog")

if __name__ == "__main__":
    app()
