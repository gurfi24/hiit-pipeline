#!/usr/bin/env python3
"""
Git credential helper backed by GITHUB_TOKEN in .env. Configured as this
repo's local credential.helper so `git push`/`pull` work non-interactively
(GIT_TERMINAL_PROMPT is disabled in this environment, and this also removes
the need for a browser login on every automated run in step 5).

Not invoked directly -- git calls it as: git_credential_helper.py <get|store|erase>
"""

import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"


def load_token():
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    # Consume stdin (the protocol=/host=/... lines) regardless of operation.
    for line in sys.stdin:
        if not line.strip():
            break

    if op == "get":
        token = load_token()
        if token:
            print("username=x-access-token")
            print(f"password={token}")
    # store/erase: no-op, we always serve the current .env value fresh.


if __name__ == "__main__":
    main()
