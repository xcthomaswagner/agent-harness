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
import threading
import urllib.error
import urllib.request
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


def _trace_watcher(
    jsonl_path: Path, config_path: Path, stop_event: threading.Event
) -> None:
    """Tail pipeline.jsonl and POST new entries to L1 for live dashboard updates.

    Runs as a daemon thread alongside the agent process. Fire-and-forget —
    failures are silently ignored so the agent is never blocked.
    """
    # Watcher log for debugging (spawn_team stdout goes to /dev/null)
    watcher_log = jsonl_path.parent / "trace-watcher.log"

    def _log(msg: str) -> None:
        try:
            with watcher_log.open("a") as lf:
                lf.write(f"{msg}\n")
        except OSError:
            pass

    if not config_path.exists():
        _log("No trace-config.json — exiting")
        return
    try:
        config = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        _log("Failed to read trace-config.json — exiting")
        return

    l1_url = config.get("l1_url", "")
    ticket_id = config.get("ticket_id", "")
    trace_id = config.get("trace_id", "")
    if not l1_url or not ticket_id:
        _log(f"Missing l1_url={l1_url!r} or ticket_id={ticket_id!r} — exiting")
        return
    _log(f"Started for {ticket_id} → {l1_url}")

    # Wait for the file to be created by the agent
    while not jsonl_path.exists() and not stop_event.is_set():
        stop_event.wait(1)
    if stop_event.is_set():
        _log("Stop event received before file appeared")
        return

    _log(f"Tailing {jsonl_path}")
    posted = 0

    def _post_entry(raw: str) -> None:
        """Parse one complete NDJSON line and POST it to L1."""
        nonlocal posted
        try:
            entry = json.loads(raw)
            entry["ticket_id"] = ticket_id
            entry["trace_id"] = trace_id
            data = json.dumps(entry).encode()
            req = urllib.request.Request(
                f"{l1_url}/api/agent-trace",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
            posted += 1
            _log(f"Posted: {entry.get('phase')}/{entry.get('event', '')[:40]}")
        except Exception as exc:
            _log(f"POST failed: {exc}")

    # Partial-line buffer. readline() against a file being actively written
    # can return a chunk without a trailing newline when it catches the
    # writer mid-flush; we accumulate those chunks until the newline arrives
    # rather than attempting to parse (and losing) the incomplete line.
    buffer = ""
    with jsonl_path.open("r") as f:
        while not stop_event.is_set():
            chunk = f.readline()
            if not chunk:
                stop_event.wait(2)  # Poll every 2 seconds
                continue
            buffer += chunk
            if not buffer.endswith("\n"):
                continue  # partial line — wait for the rest
            line, buffer = buffer.strip(), ""
            if line:
                _post_entry(line)

        # Drain pass: stop_event was set, but the subprocess may have flushed
        # its final events to the file between our last read and now. Read to
        # EOF one more time so the last few entries (pr_created, complete,
        # etc.) are not dropped.
        try:
            remainder = f.read()
        except OSError as exc:
            _log(f"Final drain read failed: {exc}")
            remainder = ""
        if remainder:
            buffer += remainder
            for raw_line in buffer.splitlines():
                stripped = raw_line.strip()
                if stripped:
                    _post_entry(stripped)

    _log(f"Stopped after posting {posted} entries")


def main() -> None:
    parser = argparse.ArgumentParser(description="Spawn an Agent Team session")
    parser.add_argument("--client-repo", required=True, help="Path to the client git repository")
    parser.add_argument("--ticket-json", required=True, help="Path to the enriched ticket JSON file")
    parser.add_argument("--branch-name", required=True, help="Branch name (e.g., ai/PROJ-123)")
    parser.add_argument("--platform-profile", default="", help="Platform profile (sitecore, salesforce)")
    parser.add_argument("--client-profile", default="", help="Client profile name (e.g., xcsf30)")
    parser.add_argument("--trace-id", default="", help="Trace ID from L1 for live trace reporting")
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

    # --- Step 0: Pre-flight cleanup — clean prior worktrees for THIS ticket only ---
    # Only removes worktrees from earlier runs of the same ticket (same branch name).
    # Other tickets' worktrees are left alone — they may be needed for debugging
    # or have completion-pending.json awaiting retry.
    worktree_dir_candidate = client_repo.parent / "worktrees" / branch_name
    if worktree_dir_candidate.exists():
        harness_dir = worktree_dir_candidate / ".harness"
        lock_file_check = harness_dir / ".agent.lock" if harness_dir.exists() else None

        # Check if an agent is actively running in this worktree
        agent_alive = False
        if lock_file_check and lock_file_check.exists():
            try:
                lock_content = lock_file_check.read_text().strip()
                lock_pid = int(lock_content) if lock_content.isdigit() else 0
                if lock_pid > 0:
                    try:
                        os.kill(lock_pid, 0)
                        agent_alive = True
                    except (ProcessLookupError, PermissionError):
                        pass
            except (OSError, ValueError):
                pass

        if agent_alive:
            print(f"[spawn] Agent already running for {branch_name} — skipping")
            sys.exit(0)

        # Worktree exists from a prior run — determine reason and clean up
        if lock_file_check and lock_file_check.exists():
            stale_reason = "agent process dead, lock file stale"
        else:
            stale_reason = "prior run completed but worktree not removed"

        # Extract ticket ID for trace
        stale_ticket_id = branch_name
        stale_ticket_json = harness_dir / "ticket.json" if harness_dir.exists() else None
        if stale_ticket_json and stale_ticket_json.exists():
            try:
                stale_ticket_id = json.loads(stale_ticket_json.read_text()).get("id", branch_name)
            except (json.JSONDecodeError, OSError):
                pass

        print(f"[spawn] Pre-flight: cleaning prior worktree for {branch_name} (ticket: {stale_ticket_id}, reason: {stale_reason})")

        # Record cleanup in the trace
        try:
            from l1_preprocessing.tracer import append_trace, generate_trace_id
            append_trace(
                stale_ticket_id,
                generate_trace_id(),
                "spawn",
                "stale_worktree_cleaned",
                worktree=str(worktree_dir_candidate),
                reason=f"Pre-flight cleanup: {stale_reason}",
            )
        except Exception:
            pass  # Trace is best-effort — don't block cleanup

        result = run_git(str(client_repo), "worktree", "remove", str(worktree_dir_candidate), "--force", check=False)
        if result.returncode != 0 and worktree_dir_candidate.exists():
            shutil.rmtree(worktree_dir_candidate, ignore_errors=True)
        run_git(str(client_repo), "worktree", "prune", check=False)
        run_git(str(client_repo), "branch", "-D", branch_name, check=False)
        print("[spawn] Prior worktree cleaned up")

    # --- Step 1: Create worktree ---
    worktree_dir = client_repo.parent / "worktrees" / branch_name
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

    # --- Step 3: Write ticket, mode, trace config, and copy attachments ---
    shutil.copy2(ticket_json, worktree_dir / ".harness" / "ticket.json")
    (worktree_dir / ".harness" / "pipeline-mode").write_text(pipeline_mode)
    print(f"[spawn] Ticket written to .harness/ticket.json (mode: {pipeline_mode})")

    # Write trace config so the file-watcher can report to L1
    with ticket_json.open() as f:
        _ticket_id = json.load(f).get("id", "")
    l1_url = os.environ.get("L1_SERVICE_URL", "http://localhost:8000")
    trace_config = {
        "ticket_id": _ticket_id,
        "trace_id": args.trace_id or "",
        "l1_url": l1_url,
    }
    trace_config_path = worktree_dir / ".harness" / "trace-config.json"
    with trace_config_path.open("w") as f:
        json.dump(trace_config, f, indent=2)
    print(f"[spawn] Trace config written (trace_id={args.trace_id[:12] or 'none'})")

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

    # Start live trace watcher — tails pipeline.jsonl and POSTs to L1
    trace_stop = threading.Event()
    _watcher_jsonl = worktree_dir / ".harness" / "logs" / "pipeline.jsonl"
    _watcher_config = worktree_dir / ".harness" / "trace-config.json"
    print(f"[spawn] Starting trace watcher (config={_watcher_config.exists()}, log={_watcher_jsonl.exists()})")
    trace_watcher = threading.Thread(
        target=_trace_watcher,
        args=(_watcher_jsonl, _watcher_config, trace_stop),
        daemon=True,
    )
    trace_watcher.start()

    # Two output files:
    #   session-stream.jsonl — full event stream including every tool use
    #     (this is what post-mortem analysis reads to verify which tools the
    #     agent called; without it session.log only has the final summary
    #     text and tool calls are invisible — see Finding 2 follow-up in
    #     session_2026_04_10_p0_p2_sf_live.md)
    #   session.log — human-readable extract of the final assistant message
    #     for quick eyeballing
    session_stream = worktree_dir / ".harness" / "logs" / "session-stream.jsonl"
    session_log = worktree_dir / ".harness" / "logs" / "session.log"
    timed_out = False
    with session_stream.open("w") as stream_file:
        try:
            proc = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--dangerously-skip-permissions",
                    "--output-format", "stream-json",
                    "--verbose",  # required by Claude Code headless when output-format=stream-json
                ],
                cwd=str(worktree_dir),
                env=env,
                stdout=stream_file,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124  # Standard timeout exit code
            print(f"[spawn] Session timed out after {timeout_seconds}s")

    # Extract the final assistant text from the stream and write it to
    # session.log for human readability. Stream events are NDJSON; look for
    # the last assistant message's text blocks.
    try:
        summary_lines: list[str] = []
        with session_stream.open() as sf:
            for line in sf:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "assistant":
                    msg = ev.get("message", {})
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                summary_lines.append(text)
        session_log.write_text(
            ("\n\n".join(summary_lines) if summary_lines else "(no assistant text in stream)")
            + "\n"
        )
    except Exception as exc:
        # Never let log extraction failure break the pipeline
        print(f"[spawn] Warning: failed to extract session.log from stream: {exc}")
        if not session_log.exists():
            session_log.write_text(f"(session.log extraction failed: {exc})\n")

    agent_lock.unlink(missing_ok=True)

    # Stop the live trace watcher (give it a moment to flush final entries)
    trace_stop.set()
    trace_watcher.join(timeout=5)
    if timed_out:
        print(f"[spawn] Session TIMED OUT after {timeout_seconds}s")
    else:
        print(f"[spawn] Session ended with exit code: {exit_code}")
    print(f"[spawn] Logs at: {session_log} (summary), {session_stream} (full stream)")

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
        "trace_id": args.trace_id or "",
        "status": status,
        "pr_url": pr_url,
        "branch": branch_name,
        "failed_units": failed_units,
        "source": ticket_source,
    }

    print(f"[spawn] Notifying L1: ticket={ticket_id} status={status} pr={pr_url} source={ticket_source}")
    try:
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

    # --- Step 6: Post-run worktree cleanup ---
    # Remove the worktree after successful runs to prevent accumulation.
    # Failed/escalated runs keep the worktree for debugging.
    # Runs where L1 notification failed keep the worktree (completion-pending.json).
    completion_pending = worktree_dir / ".harness" / "completion-pending.json"
    if status == "complete" and not completion_pending.exists():
        print(f"[spawn] Cleaning up worktree (status={status})")
        # Archive key logs to the persistent trace directory before removing
        trace_archive = client_repo.parent / "trace-archive" / ticket_id
        try:
            trace_archive.mkdir(parents=True, exist_ok=True)
            harness_logs = worktree_dir / ".harness" / "logs"
            if harness_logs.exists():
                for log_file in harness_logs.iterdir():
                    if log_file.is_file():
                        shutil.copy2(log_file, trace_archive / log_file.name)
            print(f"[spawn] Logs archived to {trace_archive}")
        except OSError as exc:
            print(f"[spawn] WARNING: Log archival failed: {exc}")

        # Remove worktree
        result = run_git(str(client_repo), "worktree", "remove", str(worktree_dir), "--force", check=False)
        if result.returncode != 0 and worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        run_git(str(client_repo), "worktree", "prune", check=False)
        print("[spawn] Worktree removed")
    elif status != "complete":
        print(f"[spawn] Keeping worktree for debugging (status={status})")
    else:
        print("[spawn] Keeping worktree (L1 notification pending)")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
