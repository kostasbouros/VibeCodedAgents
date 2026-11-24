"""Entry point: python -m agents.install_qa --mock --dry-run"""

from shared.cli import build_app

app = build_app("install_qa")

if __name__ == "__main__":
    app()
