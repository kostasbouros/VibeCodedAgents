"""Entry point: python -m agents.desk_triage --mock --dry-run"""

from shared.cli import build_app

app = build_app("desk_triage")

if __name__ == "__main__":
    app()
