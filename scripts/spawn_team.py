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

    lock_file = worktree_dir / ".harness" / ".agent.lock"

    if worktree_dir.exists():
        # Check lock file — if it exists and the PID is still alive, agent is running
        try:
            if lock_file.exists():
                lock_content = lock_file.read_text().strip()
                lock_pid = int(lock_content) if lock_content.isdigit() else 0

                # Check if the PID is still alive
                pid_alive = False
                if lock_pid > 0:
                    try:
                        os.kill(lock_pid, 0)  # signal 0 = check existence
                        pid_alive = True
                    except (ProcessLookupError, PermissionError):
                        pass

                if pid_alive:
                    print(f"[spawn] Agent running for {branch_name} (PID {lock_pid}) — skipping")
                    sys.exit(0)
                else:
                    print(f"[spawn] Stale lock (PID {lock_pid} not running) — removing")
                    lock_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            # Lock file unreadable or corrupt — safe to proceed
            lock_file.unlink(missing_ok=True)

        print("[spawn] Worktree exists but no agent running — cleaning up stale worktree")
        result = run_git(
            str(client_repo), "worktree", "remove", str(worktree_dir), "--force", check=False
        )
        if result.returncode != 0:
            print(f"[spawn] WARNING: git worktree remove failed: {result.stderr.strip()}")
            # Fallback: force remove directory
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir)
        run_git(str(client_repo), "worktree", "prune", check=False)
        run_git(str(client_repo), "branch", "-D", branch_name, check=False)
        print("[spawn] Stale worktree cleaned up")

    print(f"[spawn] Creating worktree at: {worktree_dir}")
    result = run_git(str(client_repo), "worktree", "add", str(worktree_dir), "-b", branch_name, check=False)
    if result.returncode != 0:
        run_git(str(client_repo), "worktree", "add", str(worktree_dir), branch_name)

    # --- Step 2: Inject runtime ---
    inject_args = ["python3", str(SCRIPT_DIR / "inject_runtime.py"), "--target-dir", str(worktree_dir)]
    if args.platform_profile:
        inject_args.extend(["--platform-profile", args.platform_profile])

    subprocess.run(inject_args, check=True)

    # --- Step 3: Write ticket, mode, and copy attachments ---
    shutil.copy2(ticket_json, worktree_dir / ".harness" / "ticket.json")
    (worktree_dir / ".harness" / "pipeline-mode").write_text(pipeline_mode)
    print(f"[spawn] Ticket written to .harness/ticket.json (mode: {pipeline_mode})")

    # Copy downloaded image attachments into the worktree
    with ticket_json.open() as f:
        ticket_data = json.load(f)
    attachments_dir = worktree_dir / ".harness" / "attachments"
    copied_count = 0
    for att in ticket_data.get("attachments", []):
        local_path = att.get("local_path", "")
        if local_path and Path(local_path).exists():
            attachments_dir.mkdir(parents=True, exist_ok=True)
            dest = attachments_dir / Path(local_path).name
            try:
                shutil.copy2(local_path, dest)
                att["local_path"] = str(dest)
                copied_count += 1
            except (OSError, shutil.Error) as e:
                print(f"[spawn] WARNING: Failed to copy {local_path}: {e}")
    if copied_count:
        # Re-write ticket.json with updated local_paths
        with (worktree_dir / ".harness" / "ticket.json").open("w") as f:
            json.dump(ticket_data, f, indent=2)
        print(f"[spawn] Copied {copied_count} image attachment(s) to .harness/attachments/")

    # --- Step 4: Launch Claude Code ---
    print("[spawn] Launching Claude Code session...")
    print(f"[spawn] Worktree: {worktree_dir}")
    print(f"[spawn] Branch: {branch_name}")
    print(f"[spawn] Mode: {pipeline_mode}")

    if pipeline_mode == "quick":
        prompt = (
            "You are the team lead in QUICK mode. Read the enriched ticket at "
            ".harness/ticket.json. Implement the changes yourself (do NOT spawn "
            "sub-agents). Follow the project conventions in CLAUDE.md. "
            "If the ticket has design image attachments in .harness/attachments/, "
            "read them to understand the visual design.\n\n"
            "STEP 1 — IMPLEMENT: Write code + tests. Run the full test suite. "
            "Fix failures (up to 3 attempts). Commit: feat(<ticket-id>): <description>. "
            "Do not commit .env, secrets, or harness files.\n\n"
            "STEP 2 — SELF-REVIEW: Switch roles. You are now a SEPARATE code reviewer "
            "who did NOT write this code. Be skeptical. Run git diff main...HEAD and "
            "review the diff as if someone else wrote it.\n\n"
            "Check EVERY item on this list:\n"
            "- CORRECTNESS: Does the code satisfy ALL acceptance criteria from the ticket?\n"
            "- SECURITY: dangerouslySetInnerHTML, hardcoded secrets, injection, auth gaps?\n"
            "- DEPENDENCIES: dev-only packages (ts-node, ts-jest, @types/*) in devDependencies?\n"
            "- AUTO-GENERATED FILES: Were any files committed that should be gitignored "
            "(next-env.d.ts, .next/, node_modules, dist)?\n"
            "- TEST GAPS: Every new module/component should have tests. Flag any untested code.\n"
            "- STYLE: Does the code follow project conventions from CLAUDE.md?\n"
            "- UNNECESSARY COMPLEXITY: Inline SVGs that should use a library? "
            "Duplicated logic that should be extracted?\n\n"
            "Do NOT rationalize issues away. If dangerouslySetInnerHTML is used, "
            "mark it as a warning even if the content is static — the reviewer should "
            "flag it and explain why it's acceptable, not skip it.\n\n"
            "Write findings to .harness/logs/code-review.md with format:\n"
            "## Code Review — <ticket-id>\n"
            "### Verdict: APPROVED | CHANGES_NEEDED\n"
            "### Issues Found\n"
            "- [severity: critical|warning] [category] Description — Suggestion\n"
            "### Summary\n\n"
            "If CHANGES_NEEDED with critical issues: fix them, re-run tests, "
            "amend the commit, then re-review and update the file.\n\n"
            "STEP 3 — QA MATRIX: For each acceptance criterion in the ticket, "
            "determine PASS/FAIL/NOT_TESTED with evidence. Write to "
            ".harness/logs/qa-matrix.md with format:\n"
            "## QA Matrix — <ticket-id>\n"
            "### Overall: PASS | FAIL\n"
            "### Acceptance Criteria\n"
            "| # | Criterion | Status | Evidence |\n"
            "If figma_design_spec is NOT present in the ticket, write: "
            "'Design Compliance: skipped — no Figma design spec provided'\n\n"
            "STEP 4 — SCREENSHOT: If the implementation has a visual UI, start the "
            "dev server, navigate to the page, take a browser screenshot, and save "
            "as .harness/screenshots/final.png. Skip for backend-only work.\n\n"
            "STEP 5 — PR: Push and open a draft PR. Include the code review verdict "
            "and QA matrix in the PR body.\n\n"
            "Log each step to .harness/logs/pipeline.jsonl as JSON Lines. "
            "Use actual timestamps: run 'date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ' for each entry."
        )
    else:
        prompt = (
            "You are the team lead. Read the enriched ticket at .harness/ticket.json "
            "and execute the pipeline per the Agentic Harness Pipeline Instructions in CLAUDE.md."
        )

    # Strip secrets so Claude Code uses the Max subscription and doesn't
    # leak credentials into agent sessions. Blocklist approach — remove known
    # secrets while preserving PATH, HOME, and other system vars needed by git/gh.
    _SECRET_VARS = {
        "ANTHROPIC_API_KEY",
        "JIRA_API_TOKEN",
        "ADO_PAT",
        "GITHUB_WEBHOOK_SECRET",
        "FIGMA_API_TOKEN",
        "WEBHOOK_SECRET",
        "REDIS_URL",
    }
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_VARS}

    # Write lock file before launching agent
    agent_lock = worktree_dir / ".harness" / ".agent.lock"
    agent_lock.write_text(str(os.getpid()))

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
    agent_lock.unlink(missing_ok=True)
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

    l1_url = os.environ.get("L1_SERVICE_URL", "http://localhost:8000")
    completion_data = {
        "ticket_id": ticket_id,
        "status": status,
        "pr_url": pr_url,
        "branch": branch_name,
    }

    print(f"[spawn] Notifying L1: ticket={ticket_id} status={status} pr={pr_url}")
    try:
        import urllib.request

        data = json.dumps(completion_data).encode()
        req = urllib.request.Request(
            f"{l1_url}/api/agent-complete",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        # Write to file so L1 can pick it up later on restart/poll
        backlog = worktree_dir / ".harness" / "completion-pending.json"
        backlog.write_text(json.dumps(completion_data, indent=2))
        print(f"[spawn] WARNING: Could not notify L1 — saved to {backlog}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
