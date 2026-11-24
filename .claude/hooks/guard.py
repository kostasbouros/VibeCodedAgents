"""Runs before every terminal command Claude makes. Exit code 2 blocks the command.

Blocks:
- curl / wget (and PowerShell's Invoke-WebRequest / Invoke-RestMethod) to anything but localhost
- commands that touch .env (but not .env.example), ~/.ssh or ~/.aws
"""

import json
import re
import sys

FETCH = re.compile(r"\b(curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm)\b", re.IGNORECASE)
URL = re.compile(r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3}|localhost)", re.I)
LOCAL = {"localhost", "127.0.0.1"}
SECRET_PATHS = re.compile(r"(?<![\w.])\.env(?![\w.])|[~/\\]\.ssh\b|[~/\\]\.aws\b", re.IGNORECASE)


def check(command: str) -> str | None:
    """Return a reason to block, or None if the command is fine."""
    if SECRET_PATHS.search(command):
        return "touches .env, ~/.ssh or ~/.aws, which are off limits"
    if FETCH.search(command):
        hosts = {h.lower() for h in URL.findall(command)}
        if not hosts or not hosts <= LOCAL:
            return "curl/wget is only allowed to localhost"
    return None


def main() -> None:
    data = json.load(sys.stdin)
    reason = check(data.get("tool_input", {}).get("command", ""))
    if reason:
        print(f"Blocked by .claude/hooks/guard.py: {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
