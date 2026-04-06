#!/usr/bin/env python3
"""Create Azure DevOps Service Hook subscriptions for the harness.

Usage:
    python scripts/setup-ado-webhook.py \\
        --org-url https://xc-devops.visualstudio.com \\
        --project "XC-SF-30in30" \\
        --target-url https://<ngrok-url>/webhooks/ado \\
        --pat <your-pat> \\
        [--webhook-token <shared-secret>] \\
        [--dry-run] \\
        [--manual]

Creates two Service Hook subscriptions:
  1. workitem.updated — fires when tags, state, or fields change
  2. workitem.created — fires when a new work item is created with the ai-implement tag

Requires an ADO PAT with "Service Hooks (Read & Write)" and "Project and Team (Read)" scopes.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Install with: pip install httpx", file=sys.stderr)
    sys.exit(1)


def _auth_headers(pat: str) -> dict[str, str]:
    credentials = f":{pat}"
    token = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _resolve_project_id(org_url: str, project_name: str, headers: dict[str, str]) -> str:
    """Resolve ADO project name to project ID."""
    url = f"{org_url.rstrip('/')}/_apis/projects/{project_name}?api-version=7.1"
    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    project_id: str = resp.json()["id"]
    return project_id


def _build_subscription(
    project_id: str,
    event_type: str,
    target_url: str,
    webhook_token: str,
) -> dict[str, Any]:
    """Build a Service Hook subscription body."""
    http_headers = ""
    if webhook_token:
        http_headers = f"X-ADO-Webhook-Token:{webhook_token}"

    return {
        "publisherId": "tfs",
        "eventType": event_type,
        "consumerId": "webHooks",
        "consumerActionId": "httpRequest",
        "publisherInputs": {
            "projectId": project_id,
            "areaPath": "",
            "workItemType": "",
        },
        "consumerInputs": {
            "url": target_url,
            "httpHeaders": http_headers,
            "resourceDetailsToSend": "All",
            "messagesToSend": "none",
            "detailedMessagesToSend": "none",
        },
    }


def _create_subscription(
    org_url: str, body: dict[str, Any], headers: dict[str, str], dry_run: bool
) -> str | None:
    """POST the subscription to ADO. Returns subscription ID or None on dry-run."""
    url = f"{org_url.rstrip('/')}/_apis/hooks/subscriptions?api-version=7.1"

    if dry_run:
        print(f"\n[DRY RUN] POST {url}")
        print(json.dumps(body, indent=2))
        return None

    resp = httpx.post(url, json=body, headers=headers, timeout=30.0)
    resp.raise_for_status()
    sub_id: str = resp.json()["id"]
    return sub_id


def _print_manual_instructions(
    org_url: str, project: str, target_url: str, webhook_token: str
) -> None:
    """Print step-by-step instructions for manual Service Hook creation."""
    print(f"""
=== Manual ADO Service Hook Setup ===

Project: {project}
Target URL: {target_url}

--- Subscription 1: Work Item Updated ---

1. Go to: {org_url}/{project}/_settings/serviceHooks
2. Click "+ Create subscription"
3. Select service: "Web Hooks"
4. Click "Next"
5. Trigger: "Work item updated"
   - Area path: (leave blank for all)
   - Work item type: (leave blank for all)
   - Tag: (leave blank — filtering is done by the harness)
6. Click "Next"
7. Action: "HTTP POST"
   - URL: {target_url}
   - HTTP headers: {f'X-ADO-Webhook-Token:{webhook_token}' if webhook_token else '(none)'}
   - Resource details to send: All
   - Messages to send: None
   - Detailed messages to send: None
8. Click "Test" to verify, then "Finish"

--- Subscription 2: Work Item Created ---

Repeat the above steps but select "Work item created" as the trigger in step 5.

--- Environment Variables ---

Add to your .env file:
  ADO_ORG_URL={org_url}
  ADO_PAT=<your-pat>
  ADO_WEBHOOK_TOKEN={webhook_token or '<generate-a-secret>'}
""")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create ADO Service Hook subscriptions for the harness."
    )
    parser.add_argument("--org-url", required=True, help="ADO organization URL")
    parser.add_argument("--project", required=True, help="ADO project name")
    parser.add_argument("--target-url", help="Webhook target URL (e.g. ngrok)")
    parser.add_argument("--pat", help="ADO Personal Access Token")
    parser.add_argument("--webhook-token", default="", help="Shared secret for X-ADO-Webhook-Token header")
    parser.add_argument("--dry-run", action="store_true", help="Show request bodies without sending")
    parser.add_argument("--manual", action="store_true", help="Print manual setup instructions")
    args = parser.parse_args()

    if args.manual:
        _print_manual_instructions(args.org_url, args.project, args.target_url or "<your-url>", args.webhook_token)
        return

    if not args.target_url:
        parser.error("--target-url is required (unless --manual is used)")
    if not args.pat:
        parser.error("--pat is required (unless --manual is used)")

    headers = _auth_headers(args.pat)

    # Resolve project ID
    print(f"Resolving project '{args.project}'...")
    project_id = _resolve_project_id(args.org_url, args.project, headers)
    print(f"  Project ID: {project_id}")

    event_types = ["workitem.updated", "workitem.created"]
    for event_type in event_types:
        print(f"\nCreating subscription for '{event_type}'...")
        body = _build_subscription(project_id, event_type, args.target_url, args.webhook_token)
        sub_id = _create_subscription(args.org_url, body, headers, args.dry_run)
        if sub_id:
            print(f"  Subscription ID: {sub_id}")

    if not args.dry_run:
        print("\nDone. Both subscriptions created successfully.")
    print(f"\nRemember to set these environment variables:")
    print(f"  ADO_ORG_URL={args.org_url}")
    print(f"  ADO_PAT=<your-pat>")
    if args.webhook_token:
        print(f"  ADO_WEBHOOK_TOKEN={args.webhook_token}")


if __name__ == "__main__":
    main()
