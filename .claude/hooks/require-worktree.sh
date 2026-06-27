#!/bin/bash
# PreToolUse hook: blocks Edit/Write on the main branch.
# All changes require a worktree or feature branch.

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Skip files outside the repository (plan files, global .claude/, etc.)
INPUT=$(cat)
if command -v jq >/dev/null 2>&1; then
  FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
else
  FILE_PATH=$(printf '%s' "$INPUT" | grep -oP '"file_path"\s*:\s*"\K[^"]*' || true)
fi
if [ -n "$FILE_PATH" ]; then
  REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
  case "$FILE_PATH" in
    "$REPO_ROOT"/.claude/*) exit 0 ;; # infrastructure files — always allow
    "$REPO_ROOT"/*) ;;                # file in repo — check further
    *) exit 0 ;;                      # file outside repo — skip
  esac
fi

BRANCH=$(git branch --show-current 2>/dev/null)

if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  cat >&2 <<'EOF'
BLOCKED: Editing files on the main branch is not allowed.
Use EnterWorktree before making changes.
EOF
  exit 2
fi

exit 0
