# Future Enhancements

## ai-explain Label — Ticket Clarification

**Use case:** Someone wants clarification on work that was done on a ticket. They add the `ai-explain` label to a completed ticket with a comment asking their question.

**Proposed flow:**
1. Jira automation fires webhook on `ai-explain` label added
2. L1 receives the webhook, reads the ticket + all comments + linked PR
3. L1 makes a single Claude API call (no L2 spawn, no agent team) that:
   - Reads the original ticket description and acceptance criteria
   - Reads the PR diff (fetched via GitHub API using the PR link from the completion comment)
   - Reads the latest Jira comment (the question being asked)
4. Posts a Jira comment with a plain-language explanation answering the question
5. Removes the `ai-explain` label so it doesn't re-trigger

**Why a new label instead of reopening:** Reopening a Done ticket and re-triggering `ai-implement` would kick off the full pipeline (analyst → developer → reviewer → QA → new PR), which is not what's needed. A separate label keeps the flows distinct.

**Effort:** Small — L1-only change, no L2/L3 involvement. New webhook handler + one Claude API call.

## ai-revise Label — Rework Based on Feedback

**Use case:** A PR was created but the human reviewer wants changes that go beyond what the L3 comment handler can do. They add `ai-revise` to the ticket with detailed feedback in a comment.

**Proposed flow:**
1. Jira automation fires webhook on `ai-revise` label
2. L1 reads the ticket + the latest comment (the revision request) + the existing PR
3. L1 spawns an agent on the existing branch (not a new worktree) that:
   - Reads the revision request from the Jira comment
   - Makes the requested changes on the existing PR branch
   - Runs review + QA
   - Pushes to the same PR
4. Posts a Jira comment confirming the revision is done

**Why not just re-trigger ai-implement:** That would create a new branch and new PR, losing the review history. `ai-revise` works on the existing PR.

## Ticket Re-processing (Reopened Tickets)

**Use case:** A ticket was Done, but a bug was found in the implementation. Someone moves it back to "To Do" and adds `ai-implement`.

**Current behavior:** The spawn script detects the existing worktree and cleans it up before creating a new one. A fresh pipeline run happens. This works but creates a new PR rather than updating the existing one.

**Future improvement:** Detect that a PR already exists for this ticket (check for `ai/<ticket-id>` branch). If so, work on the existing branch instead of creating a new one. This preserves review history and avoids PR proliferation.
