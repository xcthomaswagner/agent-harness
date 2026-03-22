# Agent Teams vs Sub-Agents — Architecture Decision

## Decision

The harness uses Claude Code's **`Agent()` sub-agent pattern** for multi-agent execution, not the experimental **Agent Teams** (`TeamCreate` + `SendMessage`).

## Why

**Agent Teams requires an interactive terminal session.** It spawns teammates as tmux panes or iTerm2 splits. The team lead and teammates communicate via an inbox-based messaging system (`SendMessage`) that relies on terminal UI rendering.

**Our pipeline runs headless.** The L1 service spawns agents via `claude -p` in a background subprocess with no terminal attached. This is a server-side automated pipeline triggered by Jira webhooks — there is no human watching a terminal.

The `Agent()` sub-agent pattern works in headless mode. Each sub-agent runs as a child process that executes to completion and returns its result. Multiple `Agent()` calls in a single message run concurrently. The `isolation: "worktree"` parameter gives each agent its own git copy for parallel work.

## What We Use

```
Agent(
  prompt="You are a developer...",
  mode="bypassPermissions",
  isolation="worktree"        # separate git copy for parallel devs
)
```

- Team lead spawns sub-agents sequentially or in parallel
- Each sub-agent runs to completion, writes artifacts to `.harness/logs/`
- Team lead reads the artifacts and decides the next phase
- No inter-agent messaging during execution — coordination is via files

## What Agent Teams Would Require

To use Agent Teams, we would need to:

1. Switch from `claude -p` (headless) to tmux-based session management
2. Set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
3. The team lead would use `TeamCreate` to spawn teammates in tmux panes
4. Teammates would communicate via `SendMessage` during execution
5. A human (or a monitoring script) would need to handle permission prompts that appear in teammate panes

## Known Agent Teams Issues (March 2026, 188 bug reports)

- `--dangerously-skip-permissions` doesn't bypass workspace trust prompts (#36342)
- SendMessage delivery failures — messages silently lost (#34668)
- Context window variant not inherited — teammates get 200K instead of 1M (#36670)
- bypassPermissions revokes Bash mid-session (#36225)
- MCP tool permissions not surfaced to team lead (#36007)
- TeamDelete blocked by stuck agents (#36222)
- No native headless mode support for teams

## When to Revisit

Migrate to Agent Teams when:

1. Agent Teams supports headless/non-interactive mode natively
2. The `SendMessage` delivery reliability issues are resolved
3. The permission bypass bugs are fixed
4. The feature exits "experimental" / "research preview" status

At that point, the migration path is:
- Replace `Agent()` calls in `harness-CLAUDE.md` with `TeamCreate` + `SendMessage`
- The `MESSAGE_PROTOCOL.md` abstraction layer was designed for this transition
- The file-based fallback (`/.harness/messages/`) remains as insurance

## References

- [Agent Teams Docs](https://code.claude.com/docs/en/agent-teams)
- [GitHub Issues: 188 agent teams bugs](https://github.com/anthropics/claude-code/issues?q=label%3Aarea%3Aagents+label%3Abug)
- [Anthropic: Building a C Compiler with Agent Teams](https://www.anthropic.com/engineering/building-c-compiler)
- [Addy Osmani: Claude Code Swarms](https://addyosmani.com/blog/claude-code-agent-teams/)
