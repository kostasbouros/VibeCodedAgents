"""Entry point: python -m agents.fleet_report --mock --dry-run"""

from shared.cli import build_app

app = build_app("fleet_report")

if __name__ == "__main__":
    app()
