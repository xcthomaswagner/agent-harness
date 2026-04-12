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

Creates Service Hook subscriptions:
  1. workitem.updated — fires when tags, state, or fields change
  2. workitem.created — fires when a new work item is created with the ai-implement tag
  3. git.pullrequest.created — fires when a PR is created (requires --pr-webhook-url)
  4. git.pullrequest.updated — fires when a PR is updated (requires --pr-webhook-url)
  5. build.complete — fires when a build finishes (requires --build-webhook-url)

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


def _build_pr_subscription(
    project_id: str,
    event_type: str,
    target_url: str,
    webhook_token: str,
    repository_id: str,
) -> dict[str, Any]:
    """Build a Service Hook subscription body for git PR events."""
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
            "repository": repository_id,
        },
        "consumerInputs": {
            "url": target_url,
            "httpHeaders": http_headers,
            "resourceDetailsToSend": "All",
            "messagesToSend": "none",
            "detailedMessagesToSend": "none",
        },
    }


def _build_build_subscription(
    project_id: str,
    target_url: str,
    webhook_token: str,
) -> dict[str, Any]:
    """Build a Service Hook subscription body for build.complete events."""
    http_headers = ""
    if webhook_token:
        http_headers = f"X-ADO-Webhook-Token:{webhook_token}"

    return {
        "publisherId": "tfs",
        "eventType": "build.complete",
        "consumerId": "webHooks",
        "consumerActionId": "httpRequest",
        "publisherInputs": {
            "projectId": project_id,
        },
        "consumerInputs": {
            "url": target_url,
            "httpHeaders": http_headers,
            "resourceDetailsToSend": "All",
            "messagesToSend": "none",
            "detailedMessagesToSend": "none",
        },
    }


def _resolve_repository_id(
    org_url: str, project_name: str, headers: dict[str, str]
) -> str:
    """Resolve the default repository ID for a project.

    Returns the first repository found. For projects with multiple repos,
    you may want to specify --repo-id explicitly.
    """
    url = (
        f"{org_url.rstrip('/')}/_apis/git/repositories"
        f"?project={project_name}&api-version=7.1"
    )
    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    repos = resp.json().get("value", [])
    if not repos:
        print(f"ERROR: No repositories found in project '{project_name}'", file=sys.stderr)
        sys.exit(1)
    repo_id: str = repos[0]["id"]
    print(f"  Using repository: {repos[0].get('name', '')} ({repo_id})")
    return repo_id


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
    org_url: str, project: str, target_url: str, webhook_token: str,
    pr_webhook_url: str = "", build_webhook_url: str = "",
) -> None:
    """Print step-by-step instructions for manual Service Hook creation."""
    print(f"""
=== Manual ADO Service Hook Setup ===

Project: {project}
Target URL (work items): {target_url}
PR Webhook URL: {pr_webhook_url or '(not configured)'}
Build Webhook URL: {build_webhook_url or '(not configured)'}

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

--- Subscription 3: Pull Request Created ---

1. Go to: {org_url}/{project}/_settings/serviceHooks
2. Click "+ Create subscription"
3. Select service: "Web Hooks"
4. Trigger: "Pull request created"
   - Repository: (select your repo, or leave blank for all)
5. Action: "HTTP POST"
   - URL: {pr_webhook_url or '<your-pr-webhook-url>'}
   - HTTP headers: {f'X-ADO-Webhook-Token:{webhook_token}' if webhook_token else '(none)'}
   - Resource details to send: All
6. Click "Test" to verify, then "Finish"

--- Subscription 4: Pull Request Updated ---

Same as above but select "Pull request updated" as the trigger.

--- Subscription 5: Build Completed ---

1. Go to: {org_url}/{project}/_settings/serviceHooks
2. Click "+ Create subscription"
3. Select service: "Web Hooks"
4. Trigger: "Build completed"
5. Action: "HTTP POST"
   - URL: {build_webhook_url or '<your-build-webhook-url>'}
   - HTTP headers: {f'X-ADO-Webhook-Token:{webhook_token}' if webhook_token else '(none)'}
   - Resource details to send: All
6. Click "Test" to verify, then "Finish"

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
    parser.add_argument("--target-url", help="Webhook target URL for work item events (e.g. ngrok)")
    parser.add_argument("--pr-webhook-url", help="Webhook target URL for PR events (L3 /webhooks/ado-pr)")
    parser.add_argument("--build-webhook-url", help="Webhook target URL for build events (L3 /webhooks/ado-build)")
    parser.add_argument("--repo-id", help="Repository ID for PR subscriptions (auto-resolved if omitted)")
    parser.add_argument("--pat", help="ADO Personal Access Token")
    parser.add_argument("--webhook-token", default="", help="Shared secret for X-ADO-Webhook-Token header")
    parser.add_argument("--dry-run", action="store_true", help="Show request bodies without sending")
    parser.add_argument("--manual", action="store_true", help="Print manual setup instructions")
    args = parser.parse_args()

    if args.manual:
        _print_manual_instructions(
            args.org_url, args.project,
            args.target_url or "<your-url>", args.webhook_token,
            pr_webhook_url=args.pr_webhook_url or "",
            build_webhook_url=args.build_webhook_url or "",
        )
        return

    if not args.target_url and not args.pr_webhook_url and not args.build_webhook_url:
        parser.error("At least one of --target-url, --pr-webhook-url, or --build-webhook-url is required")
    if not args.pat:
        parser.error("--pat is required (unless --manual is used)")

    headers = _auth_headers(args.pat)

    # Resolve project ID
    print(f"Resolving project '{args.project}'...")
    project_id = _resolve_project_id(args.org_url, args.project, headers)
    print(f"  Project ID: {project_id}")

    created_count = 0

    # Work item subscriptions
    if args.target_url:
        event_types = ["workitem.updated", "workitem.created"]
        for event_type in event_types:
            print(f"\nCreating subscription for '{event_type}'...")
            body = _build_subscription(project_id, event_type, args.target_url, args.webhook_token)
            sub_id = _create_subscription(args.org_url, body, headers, args.dry_run)
            if sub_id:
                print(f"  Subscription ID: {sub_id}")
            created_count += 1

    # PR subscriptions
    if args.pr_webhook_url:
        repo_id = args.repo_id
        if not repo_id:
            print(f"\nResolving default repository for '{args.project}'...")
            repo_id = _resolve_repository_id(args.org_url, args.project, headers)

        pr_events = ["git.pullrequest.created", "git.pullrequest.updated"]
        for event_type in pr_events:
            print(f"\nCreating subscription for '{event_type}'...")
            body = _build_pr_subscription(
                project_id, event_type, args.pr_webhook_url,
                args.webhook_token, repo_id,
            )
            sub_id = _create_subscription(args.org_url, body, headers, args.dry_run)
            if sub_id:
                print(f"  Subscription ID: {sub_id}")
            created_count += 1

    # Build subscription
    if args.build_webhook_url:
        print("\nCreating subscription for 'build.complete'...")
        body = _build_build_subscription(
            project_id, args.build_webhook_url, args.webhook_token,
        )
        sub_id = _create_subscription(args.org_url, body, headers, args.dry_run)
        if sub_id:
            print(f"  Subscription ID: {sub_id}")
        created_count += 1

    if not args.dry_run:
        print(f"\nDone. {created_count} subscription(s) created successfully.")
    print("\nRemember to set these environment variables:")
    print(f"  ADO_ORG_URL={args.org_url}")
    print("  ADO_PAT=<your-pat>")
    if args.webhook_token:
        print(f"  ADO_WEBHOOK_TOKEN={args.webhook_token}")


if __name__ == "__main__":
    main()
