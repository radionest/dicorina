#!/bin/bash
# PostToolUse hook: detect `gh pr create`, start background monitoring.

INPUT=$(cat)

# Detect `gh pr create` from the command field (consistent with the gate hooks);
# the PR URL is printed by the command, so it is read from the full payload below.
if command -v jq >/dev/null 2>&1; then
  COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
else
  COMMAND=$(printf '%s' "$INPUT" | grep -oP '"command"\s*:\s*"\K[^"]*' || true)
fi

echo "$COMMAND" | grep -q "gh pr create" || exit 0
echo "$COMMAND" | grep -qE "(--help|--dry-run)" && exit 0

PR_URL=$(printf '%s' "$INPUT" | grep -oP 'https://github\.com/[^"]+/pull/\d+' | head -1)
[ -z "$PR_URL" ] && exit 0

PR_NUM=$(echo "$PR_URL" | grep -oP '\d+$')
REPORT="/tmp/pr-${PR_NUM}-report.md"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detach into a new session so teardown's SIGTERM to the process group doesn't kill the watcher
if command -v setsid >/dev/null 2>&1; then
  setsid "$HOOK_DIR/pr-watch.sh" "$PR_NUM" 12 300 </dev/null >/dev/null 2>&1 &
else
  nohup "$HOOK_DIR/pr-watch.sh" "$PR_NUM" 12 300 </dev/null >/dev/null 2>&1 &
  disown
fi

cat >&2 <<EOF
PR_CREATED: PR #${PR_NUM} (${PR_URL}).
Background CI monitor started (PID $!, polling every 5 min, max 1 hour).
Report: ${REPORT}
When the user asks about PR status, read ${REPORT}.
EOF

exit 2
