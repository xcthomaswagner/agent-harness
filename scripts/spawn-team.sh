#!/usr/bin/env bash
# Wrapper around the Python canonical implementation. The shell
# variant was retired in Phase 3 of the security remediation because
# it had drifted from the Python path (lock-file semantics,
# idempotence, uncommitted-work guards, stale PID detection were
# all missing or incorrect in the Bash version).
exec python3 "$(dirname "$0")/spawn_team.py" "$@"
