"""Spawns Claude Code headless sessions for PR review, CI fixes, and comment responses."""

from __future__ import annotations

import subprocess

import structlog

logger = structlog.get_logger()


class SessionSpawner:
    """Launches Claude Code headless sessions for L3 actions."""

    def __init__(self, repo_path: str = "") -> None:
        self._repo_path = repo_path

    def spawn_pr_review(
        self, pr_number: int, pr_diff: str, ticket_context: str
    ) -> bool:
        """Spawn an Opus session for AI PR review.

        The session reads the diff and ticket context, then posts a GitHub PR review.
        """
        prompt = (
            f"You are the PR reviewer. Review PR #{pr_number}.\n\n"
            f"Follow the /pr-review skill for your review process.\n\n"
            f"Ticket context:\n{ticket_context}\n\n"
            f"Diff summary (read full diff via git):\n{pr_diff[:2000]}"
        )
        return self._spawn("pr-review", prompt, model="opus")

    def spawn_ci_fix(
        self, pr_number: int, branch: str, failure_logs: str
    ) -> bool:
        """Spawn a Sonnet session to fix CI failures.

        The session reads the failure logs, fixes the issue, and pushes to the branch.
        """
        prompt = (
            f"CI failed on PR #{pr_number}, branch {branch}.\n\n"
            f"Fix the failure and push to the same branch.\n\n"
            f"Failure logs:\n{failure_logs[:3000]}"
        )
        return self._spawn("ci-fix", prompt, model="sonnet")

    def spawn_comment_response(
        self, pr_number: int, comment_body: str, comment_author: str
    ) -> bool:
        """Spawn a session to respond to a human review comment.

        For questions: read code and reply with explanation.
        For change requests: apply the fix and push.
        """
        prompt = (
            f"Human reviewer @{comment_author} commented on PR #{pr_number}:\n\n"
            f'"{comment_body[:3000]}"\n\n'
            f"If this is a question, read the relevant code and reply.\n"
            f"If this is a change request, apply the fix, push, and reply confirming."
        )
        return self._spawn("comment-response", prompt, model="sonnet")

    def _spawn(self, session_type: str, prompt: str, model: str = "opus") -> bool:
        """Launch a Claude Code headless session."""
        cmd = ["claude", "-p", prompt]
        if model == "sonnet":
            cmd.extend(["--model", "sonnet"])

        log = logger.bind(session_type=session_type, model=model)

        try:
            subprocess.Popen(
                cmd,
                cwd=self._repo_path or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("session_spawned")
            return True
        except FileNotFoundError:
            log.error("claude_cli_not_found")
            return False
        except OSError as exc:
            log.error("session_spawn_failed", error=str(exc))
            return False
