#!/usr/bin/env bash
# check_coverage.sh — Extract test coverage percentage from common test runners.
#
# Usage: bash scripts/check_coverage.sh [--framework jest|pytest|vitest]
#
# Auto-detects the framework if not specified.

set -euo pipefail

FRAMEWORK=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --framework) FRAMEWORK="$2"; shift 2 ;;
        --help) echo "Usage: $0 [--framework jest|pytest|vitest]"; exit 0 ;;
        *) shift ;;
    esac
done

# Auto-detect framework
if [[ -z "$FRAMEWORK" ]]; then
    if [[ -f "jest.config.ts" ]] || [[ -f "jest.config.js" ]]; then
        FRAMEWORK="jest"
    elif [[ -f "vitest.config.ts" ]] || [[ -f "vitest.config.js" ]]; then
        FRAMEWORK="vitest"
    elif [[ -f "pyproject.toml" ]] || [[ -f "setup.cfg" ]]; then
        FRAMEWORK="pytest"
    else
        echo "ERROR: Could not auto-detect test framework"
        exit 1
    fi
fi

echo "Framework: $FRAMEWORK"

case "$FRAMEWORK" in
    jest)
        npx jest --coverage --coverageReporters=text-summary 2>&1 | grep -E "Statements|Branches|Functions|Lines"
        ;;
    vitest)
        npx vitest run --coverage 2>&1 | grep -E "Statements|Branches|Functions|Lines"
        ;;
    pytest)
        pytest --cov --cov-report=term-missing 2>&1 | grep -E "TOTAL|^Name"
        ;;
    *)
        echo "ERROR: Unknown framework: $FRAMEWORK"
        exit 1
        ;;
esac
