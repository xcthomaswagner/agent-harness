# Azure Repos Support — Status

This document tracks the work to support Azure Repos as source control
(as opposed to GitHub) for ADO-connected projects.

Phase A (current) connects ADO work items to projects that use **GitHub** for
source control. Phase B adds full **Azure Repos** support via ADO MCP tools.

## Completed (Phase B — feature/ado-mcp-integration)

### Configuration Foundation (Phase 1)
- [x] Client profile schema: `ado_project` and `ado_repository_id` fields under `source_control`
- [x] ClientProfile class: `source_control_type`, `is_azure_repos`, `ado_project`, `ado_repository_id` properties
- [x] `find_profile_by_ado_repo()` for L3 profile resolution
- [x] `spawn_team.py --client-profile`: writes `.harness/source-control.json` to worktree
- [x] `pipeline.py._trigger_l2()`: passes `--client-profile` to spawn script
- [x] Completion payload includes `"source": "ado"` for correct adapter routing
- [x] Git remote URL rewritten with PAT auth for Azure Repos worktrees
- [x] ADO MCP tools verified available in headless `claude -p` sessions (managed MCP)

### L2 Agent Instructions (Phase 2)
- [x] `harness-CLAUDE.md`: conditional PR creation (gh vs `mcp__ado__repo_create_pull_request`)
- [x] `pr-review/SKILL.md`: conditional review posting (gh vs `mcp__ado__repo_create_pull_request_thread`)
- [x] `merge-coordinator.md`: conditional draft PR creation
- [x] `quick-mode-prompt.md`: directs to source-control.json
- [x] All `gh pr/api/issue` references audited and made conditional

### L3 PR Review (Phase 3)
- [x] `ado_event_classifier.py`: maps ADO Service Hook PR events to EventType
- [x] `ado_api.py`: REST client for ADO PR state + completion
- [x] `POST /webhooks/ado-pr`: endpoint with token validation, event classification, routing
- [x] Ticket ID extraction from ADO branch names (`refs/heads/ai/TICKET-123`)

### L1 Enhancements (Phase 4)
- [x] `ado_adapter.link_work_item_to_pr()`: links work item to PR via ArtifactLink
- [x] `agent_complete` handler calls link on ADO completions

## Remaining (Future Work)

### L3 — Spawner Integration
- [ ] Wire `SessionSpawner` to support `source_control_type="azure-repos"` in prompts
- [ ] ADO-aware review prompts referencing MCP tools instead of `gh pr`
- [ ] Per-profile repo path resolution (not hardcoded `CLIENT_REPO_PATH`)

### L3 — Full Event Handling
- [ ] Handle `REVIEW_APPROVED`, `REVIEW_CHANGES_REQUESTED`, `REVIEW_COMMENT` ADO events
- [ ] Handle `CI_FAILED`/`CI_PASSED` from Azure Pipelines `build.complete` webhooks
- [ ] Autonomy event forwarding for ADO PR actions

### Auto-Merge
- [ ] Wire `auto_merge.py` to use `ado_api.complete_ado_pr()` for Azure Repos PRs

### Setup Tooling
- [ ] `setup-ado-webhook.py`: add `git.pullrequest.created/updated` Service Hook subscriptions

### Other
- [ ] ADO attachment download in L1 pipeline (currently skipped)
- [ ] ADO screenshot upload on agent-complete
- [ ] E2E testing against a real Azure Repos instance
