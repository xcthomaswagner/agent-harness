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
sys.path.insert(0, str(SCRIPT_DIR.parent / "services"))

from shared.env_sanitize import sanitized_env  # noqa: E402


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
    parser.add_argument("--client-profile", default="", help="Client profile name (e.g., xcsf30)")
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

    # Set git identity in worktree so commits come from the agent account
    agent_name = os.environ.get("AGENT_GIT_NAME", "XCentium Agent")
    agent_email = os.environ.get("AGENT_GIT_EMAIL", "xcagent.rockwell@xcentium.com")
    run_git(str(worktree_dir), "config", "user.name", agent_name)
    run_git(str(worktree_dir), "config", "user.email", agent_email)
    print(f"[spawn] Git identity: {agent_name} <{agent_email}>")

    # --- Step 2: Inject runtime ---
    inject_args = ["python3", str(SCRIPT_DIR / "inject_runtime.py"), "--target-dir", str(worktree_dir)]
    if args.platform_profile:
        inject_args.extend(["--platform-profile", args.platform_profile])

    subprocess.run(inject_args, check=True)

    # Verify injection created CLAUDE.md (critical for agent operation)
    claude_md = worktree_dir / "CLAUDE.md"
    if not claude_md.exists():
        print(f"[spawn] ERROR: CLAUDE.md not found at {claude_md} after injection")
        sys.exit(1)

    # --- Step 2b: Write source control context from client profile ---
    profile = None
    if args.client_profile:
        from l1_preprocessing.client_profile import load_profile

        profile = load_profile(args.client_profile)
        if profile:
            sc = profile.source_control
            sc_context = {
                "type": profile.source_control_type,
                "org": sc.get("org", ""),
                "repo": sc.get("repo", ""),
                "default_branch": sc.get("default_branch", "main"),
                "branch_prefix": sc.get("branch_prefix", "ai/"),
                "ado_project": profile.ado_project,
                "ado_repository_id": profile.ado_repository_id,
            }
            sc_path = worktree_dir / ".harness" / "source-control.json"
            sc_path.parent.mkdir(parents=True, exist_ok=True)
            with sc_path.open("w") as f:
                json.dump(sc_context, f, indent=2)
            print(f"[spawn] Source control context written ({profile.source_control_type})")

            # Rewrite git remote for Azure Repos PAT auth
            if profile.is_azure_repos:
                ado_pat = os.environ.get("ADO_PAT", "")
                org_url = sc.get("org", "")  # e.g., https://dev.azure.com/myorg
                ado_project = profile.ado_project
                repo_name = sc.get("repo", "")
                if ado_pat and org_url and ado_project and repo_name:
                    # Strip protocol and trailing slash for URL construction
                    host = org_url.replace("https://", "").replace("http://", "").rstrip("/")
                    auth_url = f"https://ado-agent:{ado_pat}@{host}/{ado_project}/_git/{repo_name}"
                    result = run_git(str(worktree_dir), "remote", "set-url", "origin", auth_url, check=False)
                    if result.returncode != 0:
                        print("[spawn] ERROR: Failed to set Azure Repos remote URL")
                    else:
                        print(f"[spawn] Azure Repos remote set: {host}/{ado_project}/_git/{repo_name}")
                else:
                    missing = []
                    if not ado_pat:
                        missing.append("ADO_PAT")
                    if not org_url:
                        missing.append("source_control.org")
                    if not ado_project:
                        missing.append("source_control.ado_project")
                    if not repo_name:
                        missing.append("source_control.repo")
                    print(f"[spawn] WARNING: Azure Repos auth incomplete, missing: {', '.join(missing)}")
        else:
            print(f"[spawn] WARNING: Client profile '{args.client_profile}' not found")

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
        # Read quick-mode instructions from the shared file (single source of truth)
        quick_prompt_file = SCRIPT_DIR.parent / "runtime" / "quick-mode-prompt.md"
        prompt = quick_prompt_file.read_text()
    else:
        prompt = (
            "You are the team lead. Read the enriched ticket at .harness/ticket.json "
            "and execute the pipeline per the Agentic Harness Pipeline Instructions in CLAUDE.md."
        )

    env = sanitized_env()

    # Session timeout: prevent runaway agents from holding resources indefinitely.
    # Quick mode: 30 minutes. Multi mode: 90 minutes. Override via AGENT_TIMEOUT_SECONDS.
    default_timeout = 1800 if pipeline_mode == "quick" else 5400
    timeout_seconds = int(os.environ.get("AGENT_TIMEOUT_SECONDS", str(default_timeout)))

    # Write lock file before launching agent
    agent_lock = worktree_dir / ".harness" / ".agent.lock"
    agent_lock.write_text(str(os.getpid()))

    session_log = worktree_dir / ".harness" / "logs" / "session.log"
    timed_out = False
    with session_log.open("w") as log_file:
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--dangerously-skip-permissions"],
                cwd=str(worktree_dir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124  # Standard timeout exit code
            print(f"[spawn] Session timed out after {timeout_seconds}s")

    agent_lock.unlink(missing_ok=True)
    if timed_out:
        print(f"[spawn] Session TIMED OUT after {timeout_seconds}s")
    else:
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

    # Extract failed/blocked units from pipeline log
    failed_units = []
    if pipeline_jsonl.exists() and status in ("partial", "escalated"):
        for line in pipeline_jsonl.read_text().splitlines():
            try:
                entry = json.loads(line)
                ev = entry.get("event", "")
                if "blocked" in ev.lower() or "failed" in ev.lower():
                    unit_id = entry.get("unit", entry.get("unit_id", ev))
                    failed_units.append({
                        "unit_id": str(unit_id),
                        "description": entry.get("event", ""),
                        "failure_reason": entry.get("reason", entry.get("error", "Unknown")),
                    })
            except json.JSONDecodeError:
                continue

    l1_url = os.environ.get("L1_SERVICE_URL", "http://localhost:8000")
    # Derive ticket source from profile so L1 routes to the right adapter
    ticket_source = "jira"
    if profile and profile.ticket_source_type:
        ticket_source = profile.ticket_source_type

    completion_data: dict[str, object] = {
        "ticket_id": ticket_id,
        "status": status,
        "pr_url": pr_url,
        "branch": branch_name,
        "failed_units": failed_units,
        "source": ticket_source,
    }

    print(f"[spawn] Notifying L1: ticket={ticket_id} status={status} pr={pr_url} source={ticket_source}")
    try:
        import urllib.error
        import urllib.request

        data = json.dumps(completion_data).encode()
        req = urllib.request.Request(
            f"{l1_url}/api/agent-complete",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        # L1 responded with an error — likely a code bug, not transient
        print(f"[spawn] ERROR: L1 returned HTTP {exc.code}: {exc.reason}")
        backlog = worktree_dir / ".harness" / "completion-pending.json"
        backlog.write_text(json.dumps(completion_data, indent=2))
        print(f"[spawn] Saved completion data to {backlog}")
    except (urllib.error.URLError, OSError) as exc:
        # Network/connection error — transient, L1 may be down
        print(f"[spawn] WARNING: Could not reach L1: {exc}")
        backlog = worktree_dir / ".harness" / "completion-pending.json"
        backlog.write_text(json.dumps(completion_data, indent=2))
        print(f"[spawn] Saved completion data to {backlog}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
