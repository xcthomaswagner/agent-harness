#!/usr/bin/env python3
"""Bypass L1 and spawn an Agent Team session directly.

Useful for testing L2 in isolation without running the webhook service.
Extracts the ticket ID from the ticket JSON and delegates to
``spawn_team.py`` with ``--branch-name ai/<ticket-id>``.

Usage:
    python scripts/direct_spawn.py \\
        --client-repo <path> \\
        --ticket-json <path> \\
        [--platform-profile <name>]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Directly spawn an Agent Team session (bypasses L1)"
    )
    parser.add_argument(
        "--client-repo", required=True, help="Path to the client git repository"
    )
    parser.add_argument(
        "--ticket-json", required=True, help="Path to the enriched ticket JSON file"
    )
    parser.add_argument(
        "--platform-profile", default="", help="Platform profile (sitecore, salesforce)"
    )
    args = parser.parse_args()

    client_repo = Path(args.client_repo).resolve()
    ticket_json = Path(args.ticket_json).resolve()

    if not ticket_json.exists():
        print(f"Error: Ticket JSON file not found: {ticket_json}", file=sys.stderr)
        sys.exit(1)

    # Extract ticket ID from JSON for branch name
    try:
        with ticket_json.open() as f:
            ticket_data = json.load(f)
        ticket_id = ticket_data["id"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Error: Cannot read ticket ID from JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    branch_name = f"ai/{ticket_id}"
    print(f"Direct spawn: ticket={ticket_id}, branch={branch_name}")

    script_dir = Path(__file__).resolve().parent
    spawn_team = script_dir / "spawn_team.py"

    cmd = [
        sys.executable,
        str(spawn_team),
        "--client-repo",
        str(client_repo),
        "--ticket-json",
        str(ticket_json),
        "--branch-name",
        branch_name,
    ]
    if args.platform_profile:
        cmd.extend(["--platform-profile", args.platform_profile])

    # exec the Python script so caller sees its stdout/stderr/exit code
    # directly — no intermediate shell swallowing signals.
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
