"""Entry point: python -m agents.account_signals --mock --dry-run"""

from shared.cli import build_app

app = build_app("account_signals")

if __name__ == "__main__":
    app()
