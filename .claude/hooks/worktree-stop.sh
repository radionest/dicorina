#!/bin/bash
# Stop hook: blocks session end in a worktree,
# so Claude asks the user what to do with changes.

INPUT=$(cat)

# Prevent infinite loop — skip on retry
if echo "$INPUT" | grep -qE '"stop_hook_active"\s*:\s*true'; then
  exit 0
fi

# Debounce: don't block again within 120 seconds (key on session_id to avoid PID-reuse collisions)
if command -v jq >/dev/null 2>&1; then
  SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
fi
[ -z "${SESSION_ID:-}" ] && SESSION_ID="$PPID"
DEBOUNCE_FILE="/tmp/claude-worktree-stop-${SESSION_ID}"
if [ -f "$DEBOUNCE_FILE" ]; then
  LAST=$(cat "$DEBOUNCE_FILE" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  if [ $((NOW - LAST)) -lt 120 ]; then
    exit 0
  fi
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Check that this is a worktree (not the main repo)
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
if [ "$GIT_DIR" = "$GIT_COMMON_DIR" ]; then
  exit 0
fi

BRANCH=$(git branch --show-current 2>/dev/null)
HAS_CHANGES=$(git status --porcelain 2>/dev/null | head -1)
AHEAD=$(git rev-list main..HEAD --count 2>/dev/null || git rev-list master..HEAD --count 2>/dev/null || echo "0")

date +%s > "$DEBOUNCE_FILE"

cat >&2 <<EOF
WORKTREE_PENDING: Session is in a worktree, branch '$BRANCH'.
Commits ahead of main: $AHEAD
Uncommitted changes: $([ -n "$HAS_CHANGES" ] && echo "yes" || echo "no")

Before ending, ask the user:
1) Push branch + create PR to main + ExitWorktree(keep)
2) Keep worktree (to continue later) — ExitWorktree(keep)
3) Discard changes + remove worktree — ExitWorktree(remove, discard_changes=true)
EOF

exit 2
