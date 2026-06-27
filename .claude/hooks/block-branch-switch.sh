#!/bin/bash
# PreToolUse hook for Bash: blocks branch switching in the root project directory.
# Use EnterWorktree to work on a different branch.

INPUT=$(cat)
if command -v jq >/dev/null 2>&1; then
  COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
else
  COMMAND=$(printf '%s' "$INPUT" | grep -oP '"command"\s*:\s*"\K[^"]*' || true)
fi
[ -z "$COMMAND" ] && exit 0

# Split on shell separators so a branch-switch hidden in a chain is still caught
# (e.g. `git checkout main && git checkout -- file`).
SEGMENTS=$(printf '%s' "$COMMAND" | tr ';&|' '\n')
TOTAL=$(printf '%s\n' "$SEGMENTS" | grep -cP 'git\s+(checkout|switch)\b' || true)
[ "$TOTAL" -eq 0 ] && exit 0

# Allow only if EVERY checkout/switch segment is a file-restore (`-- <file>`)
RESTORES=$(printf '%s\n' "$SEGMENTS" | grep -P 'git\s+(checkout|switch)\b' | grep -cP '\s--(\s|$)' || true)
[ "$TOTAL" -eq "$RESTORES" ] && exit 0

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Check that we're in the root directory (not a worktree)
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)

if [ "$(realpath "$GIT_DIR")" = "$(realpath "$COMMON_DIR")" ]; then
  cat >&2 <<'EOF'
BLOCKED: Branch switching in the root project directory is not allowed.
Use EnterWorktree to work on a different branch.
`git checkout -- <file>` for restoring files is still available.
EOF
  exit 2
fi

exit 0
