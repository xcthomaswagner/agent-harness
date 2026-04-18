#!/usr/bin/env bash
# Wrapper around the Python canonical implementation. The shell
# variant was retired in Phase 3 of the security remediation to
# consolidate all spawn logic in one codebase.
exec python3 "$(dirname "$0")/direct_spawn.py" "$@"
