# Composio Jira Tracker Plugin

## Overview

Composio supports tracker plugins (GitHub, Linear, GitLab) that let the orchestrator agent read issues and spawn sessions based on them. A Jira tracker plugin would let `ao spawn SCRUM-5` read the ticket directly from Jira instead of requiring our L1 service as an intermediary.

## Plugin Interface

Based on Composio's plugin architecture (`packages/core/src/types.ts`):

```typescript
interface TrackerPlugin {
  name: string;

  // Fetch an issue by identifier
  getIssue(id: string): Promise<Issue>;

  // List issues matching criteria
  listIssues(options: ListOptions): Promise<Issue[]>;

  // Update issue status
  updateIssue(id: string, update: IssueUpdate): Promise<void>;

  // Post a comment
  addComment(id: string, body: string): Promise<void>;
}
```

## Implementation Plan

### Directory Structure
```
packages/plugins/tracker-jira/
├── src/
│   ├── index.ts        # Plugin export
│   ├── jira-client.ts  # Jira REST API client
│   └── mapper.ts       # Map Jira issue → Composio Issue
├── package.json
└── tsconfig.json
```

### Jira Client
```typescript
class JiraClient {
  constructor(
    private baseUrl: string,
    private email: string,
    private apiToken: string,
  ) {}

  async getIssue(key: string): Promise<JiraIssue> {
    const resp = await fetch(
      `${this.baseUrl}/rest/api/3/issue/${key}`,
      { headers: this.authHeaders() }
    );
    return resp.json();
  }

  // ... listIssues, updateIssue, addComment
}
```

### Configuration
```yaml
# agent-orchestrator.yaml
projects:
  my-project:
    tracker:
      plugin: jira
      baseUrl: https://company.atlassian.net
      projectKey: PROJ
      email: bot@company.com
      # apiToken from JIRA_API_TOKEN env var
```

## Current Workaround

Until the plugin is built, our L1 Pre-Processing Service handles Jira integration:
- Jira automation rule fires webhook to L1
- L1 enriches the ticket and spawns the agent
- This is actually MORE capable than a tracker plugin because it includes the analyst enrichment step

The Jira plugin would be useful for:
- Using `ao spawn SCRUM-5` directly from the CLI
- Dashboard showing Jira ticket details alongside session status
- Two-way status sync managed by Composio instead of our custom code
