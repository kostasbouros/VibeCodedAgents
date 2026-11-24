"""Entry point: python -m agents.install_verifier --mock --dry-run"""

from shared.cli import build_app

app = build_app("install_verifier")

if __name__ == "__main__":
    app()
