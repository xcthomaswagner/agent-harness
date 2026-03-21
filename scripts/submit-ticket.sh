#!/usr/bin/env bash
# submit-ticket.sh — Submit a ticket JSON file to the L1 service for processing.
#
# Usage:
#   ./scripts/submit-ticket.sh <ticket-json-file> [--url <service-url>]

set -euo pipefail

URL="http://localhost:8000/api/process-ticket"
TICKET_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --url) URL="$2"; shift 2 ;;
        --help) echo "Usage: $0 <ticket-json-file> [--url <service-url>]"; exit 0 ;;
        *) TICKET_FILE="$1"; shift ;;
    esac
done

if [[ -z "$TICKET_FILE" ]]; then
    echo "Error: Ticket JSON file path required"
    echo "Usage: $0 <ticket-json-file> [--url <service-url>]"
    exit 1
fi

if [[ ! -f "$TICKET_FILE" ]]; then
    echo "Error: File not found: $TICKET_FILE"
    exit 1
fi

echo "Submitting ticket to $URL..."
curl -s -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d @"$TICKET_FILE" | python3 -m json.tool

echo ""
echo "Done."
