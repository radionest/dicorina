#!/bin/bash
# PreToolUse hook for Agent: blocks development agents on main.
# Forces entering a worktree so analysis and edits happen in the same context.

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

BRANCH=$(git branch --show-current 2>/dev/null)
[ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ] && exit 0

INPUT=$(cat)
if command -v jq >/dev/null 2>&1; then
  SUBAGENT=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null)
else
  SUBAGENT=$(printf '%s' "$INPUT" | grep -oP '"subagent_type"\s*:\s*"\K[^"]*' || true)
fi

# Allowlist read-only agents; default-deny everything else (incl. empty subagent_type).
# Exact-match by design — keep this list in sync with agent names; write-capable
# agents (e.g. general-purpose) stay blocked even for read-only use.
case "$SUBAGENT" in
  Explore|Plan|feature-dev:code-explorer|feature-dev:code-reviewer|claude-code-guide)
    exit 0 ;;
esac

cat >&2 <<'EOF'
BLOCKED: This agent can modify files and may not run on the main branch.
Use EnterWorktree before launching development or architecture agents.
EOF
exit 2
