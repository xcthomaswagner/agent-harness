# Azure Repos Support — Future Work

This document tracks the remaining work to support Azure Repos as source control
(as opposed to GitHub) for ADO-connected projects.

Phase A (current) connects ADO work items to projects that use **GitHub** for
source control. The items below are needed when a project uses **Azure Repos**.

## TODO

### L3 — PR Review Service
- [ ] Webhook handler for Azure Repos PR events (`git.pullrequest.created`, `git.pullrequest.updated`)
- [ ] Normalize Azure Repos PR payload into the shared PR model used by L3
- [ ] PR review comment posting via Azure Repos API (`POST /_apis/git/repositories/{repo}/pullRequests/{id}/threads`)
- [ ] CI status integration (Azure Pipelines `build.complete` webhook → map to check-run model)

### L2 — Agent Execution
- [ ] Azure Repos merge API integration (`POST /_apis/git/repositories/{repo}/pullRequests/{id}` with `status: completed`)
- [ ] Auto-merge policy enforcement (merge policies differ from GitHub branch protection)
- [ ] PR creation via Azure Repos API (currently uses `gh pr create`)

### Autonomy Engine
- [ ] Event forwarding from Azure Repos PR webhooks to `/api/autonomy/event`
- [ ] Map Azure Repos review states (Approved, Approved with suggestions, etc.) to the autonomy event model

### spawn_team.py
- [ ] Support `source_control.type: azure-repos` in client profiles
- [ ] Clone via Azure Repos HTTPS URL with PAT auth
- [ ] Push branches to Azure Repos remote

### Client Profile Schema
- [ ] Document `source_control.type: azure-repos` fields (org URL, project, repo name)
- [ ] Add `source_control.ado_repo_id` for API calls that need the repo GUID
