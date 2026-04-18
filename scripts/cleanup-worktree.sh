#!/usr/bin/env bash
# Wrapper around the Python canonical implementation. The shell
# variant was retired in Phase 3 of the security remediation because
# it had drifted from the Python path (lock-file semantics,
# idempotence, uncommitted-work guards were all missing).
exec python3 "$(dirname "$0")/cleanup_worktree.py" "$@"
