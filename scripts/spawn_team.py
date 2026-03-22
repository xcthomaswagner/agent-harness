#!/usr/bin/env python3
"""Spawn an Agent Team — create worktree, inject runtime, launch Claude Code.

This is the bridge between L1 (pre-processing service) and L2 (Agent Team execution).

Usage:
    python scripts/spawn_team.py \
        --client-repo <path> \
        --ticket-json <path> \
        --branch-name <name> \
        [--platform-profile <name>] \
        [--mode multi|quick]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def run_git(client_repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the client repo."""
    return subprocess.run(
        ["git", "-C", client_repo, *args],
        capture_output=True, text=True, check=check,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Spawn an Agent Team session")
    parser.add_argument("--client-repo", required=True, help="Path to the client git repository")
    parser.add_argument("--ticket-json", required=True, help="Path to the enriched ticket JSON file")
    parser.add_argument("--branch-name", required=True, help="Branch name (e.g., ai/PROJ-123)")
    parser.add_argument("--platform-profile", default="", help="Platform profile (sitecore, salesforce)")
    parser.add_argument("--mode", default="multi", choices=["multi", "quick"], help="Pipeline mode")
    args = parser.parse_args()

    client_repo = Path(args.client_repo).resolve()
    ticket_json = Path(args.ticket_json).resolve()
    branch_name = args.branch_name
    pipeline_mode = args.mode

    # --- Validate inputs ---
    if not (client_repo / ".git").exists() and not (client_repo / ".git").is_file():
        print(f"Error: Not a git repository: {client_repo}")
        sys.exit(1)

    if not ticket_json.exists():
        print(f"Error: Ticket JSON file not found: {ticket_json}")
        sys.exit(1)

    with ticket_json.open() as f:
        try:
            json.load(f)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in ticket file: {ticket_json}")
            sys.exit(1)

    # --- Step 1: Create worktree (handle collisions) ---
    worktree_dir = client_repo.parent / "worktrees" / branch_name

    if worktree_dir.exists():
        print("[spawn] Worktree already exists — cleaning up previous run")

        # Kill any running agent for this branch
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"claude.*{branch_name}"],
                capture_output=True, text=True, check=False,
            )
            for pid in result.stdout.strip().split("\n"):
                if pid:
                    print(f"[spawn] Killing existing agent (PID: {pid})")
                    subprocess.run(["kill", pid], check=False)
            if result.stdout.strip():
                time.sleep(2)
        except FileNotFoundError:
            # pgrep not available (Windows) — skip
            pass

        run_git(str(client_repo), "worktree", "remove", str(worktree_dir), "--force", check=False)
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir)
        run_git(str(client_repo), "worktree", "prune", check=False)
        run_git(str(client_repo), "branch", "-D", branch_name, check=False)
        print("[spawn] Previous worktree cleaned up")

    print(f"[spawn] Creating worktree at: {worktree_dir}")
    result = run_git(str(client_repo), "worktree", "add", str(worktree_dir), "-b", branch_name, check=False)
    if result.returncode != 0:
        run_git(str(client_repo), "worktree", "add", str(worktree_dir), branch_name)

    # --- Step 2: Inject runtime ---
    inject_args = ["python3", str(SCRIPT_DIR / "inject_runtime.py"), "--target-dir", str(worktree_dir)]
    if args.platform_profile:
        inject_args.extend(["--platform-profile", args.platform_profile])

    subprocess.run(inject_args, check=True)

    # --- Step 3: Write ticket and mode ---
    shutil.copy2(ticket_json, worktree_dir / ".harness" / "ticket.json")
    (worktree_dir / ".harness" / "pipeline-mode").write_text(pipeline_mode)
    print(f"[spawn] Ticket written to .harness/ticket.json (mode: {pipeline_mode})")

    # --- Step 4: Launch Claude Code ---
    print("[spawn] Launching Claude Code session...")
    print(f"[spawn] Worktree: {worktree_dir}")
    print(f"[spawn] Branch: {branch_name}")
    print(f"[spawn] Mode: {pipeline_mode}")

    if pipeline_mode == "quick":
        prompt = (
            "You are the team lead in QUICK mode. Read the enriched ticket at "
            ".harness/ticket.json. Implement the changes yourself (do NOT spawn "
            "sub-agents). Write tests, run them, commit, push, and open a draft PR. "
            "Follow the project conventions in CLAUDE.md. Use conventional commits: "
            "feat(<ticket-id>): <description>. Do not commit .env, secrets, or harness files."
        )
    else:
        prompt = (
            "You are the team lead. Read the enriched ticket at .harness/ticket.json "
            "and execute the pipeline per the Agentic Harness Pipeline Instructions in CLAUDE.md."
        )

    # Strip ANTHROPIC_API_KEY so Claude Code uses the Max subscription
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    session_log = worktree_dir / ".harness" / "logs" / "session.log"
    with session_log.open("w") as log_file:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            cwd=str(worktree_dir),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    exit_code = proc.returncode
    print(f"[spawn] Session ended with exit code: {exit_code}")
    print(f"[spawn] Logs at: {session_log}")

    # --- Step 5: Notify L1 of completion ---
    try:
        with (worktree_dir / ".harness" / "ticket.json").open() as f:
            ticket_id = json.load(f).get("id", "unknown")
    except Exception:
        ticket_id = "unknown"

    # Extract PR URL from pipeline log
    pr_url = ""
    pipeline_jsonl = worktree_dir / ".harness" / "logs" / "pipeline.jsonl"
    if pipeline_jsonl.exists():
        for line in pipeline_jsonl.read_text().splitlines():
            match = re.search(r'"pr_url":\s*"(https://[^"]+)"', line)
            if match:
                pr_url = match.group(1)

    if exit_code == 0 and pr_url:
        status = "complete"
    elif exit_code == 0:
        status = "partial"
    else:
        status = "escalated"

    print(f"[spawn] Notifying L1: ticket={ticket_id} status={status} pr={pr_url}")
    try:
        import urllib.request

        data = json.dumps({
            "ticket_id": ticket_id,
            "status": status,
            "pr_url": pr_url,
            "branch": branch_name,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8000/api/agent-complete",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        print("[spawn] WARNING: Could not notify L1 (service may not be running)")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
