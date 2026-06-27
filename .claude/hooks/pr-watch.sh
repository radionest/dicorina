#!/bin/bash
# Background PR monitor — polls GitHub CI checks and writes report.
# Usage: pr-watch.sh <PR_NUMBER> [max_iterations] [interval_seconds]

set -euo pipefail

PR_NUM="${1:?Usage: pr-watch.sh <PR_NUMBER>}"
MAX_ITER="${2:-12}"
INTERVAL="${3:-300}"
REPORT="/tmp/pr-${PR_NUM}-report.md"

echo "# PR #${PR_NUM} — Monitoring started $(date -Iseconds)" > "$REPORT"
echo "Checking every ${INTERVAL}s, max ${MAX_ITER} iterations." >> "$REPORT"

for ((i=1; i<=MAX_ITER; i++)); do
    # Poll first, sleep at the end — otherwise the report is delayed a full interval.
    if CHECKS=$(gh pr checks "$PR_NUM" 2>&1); then RC=0; else RC=$?; fi
    STATUS_JSON=$(gh pr view "$PR_NUM" --json statusCheckRollup,comments,reviews,state 2>&1) || true

    # gh pr checks exit codes: 0=all pass, 1=some failed, 8=pending, other=error.
    # Right after `gh pr create` checks aren't registered yet — gh prints
    # "no checks reported" and exits non-zero. Treat that as not-ready (keep
    # waiting), else a false "CI Complete" lands before CI even starts.
    if echo "$CHECKS" | grep -qi 'no checks reported'; then
        echo "Iteration ${i}/${MAX_ITER}: no checks registered yet ($(date -Iseconds))" >> "$REPORT"
    elif [ "$RC" -eq 0 ] || [ "$RC" -eq 1 ]; then
        cat > "$REPORT" <<REPORT_EOF
# PR #${PR_NUM} — CI Complete ($(date -Iseconds))

## Check Results
\`\`\`
${CHECKS}
\`\`\`

## Reviews & Comments
\`\`\`json
${STATUS_JSON}
\`\`\`
REPORT_EOF
        exit 0
    else
        echo "Iteration ${i}/${MAX_ITER}: checks pending (rc=${RC}, $(date -Iseconds))" >> "$REPORT"
    fi

    sleep "$INTERVAL"
done

cat > "$REPORT" <<REPORT_EOF
# PR #${PR_NUM} — Monitoring Timeout ($(date -Iseconds))

Gave up after ${MAX_ITER} iterations ($(( MAX_ITER * INTERVAL / 60 )) minutes).

## Last Check Results
\`\`\`
${CHECKS:-no data}
\`\`\`

## Reviews & Comments
\`\`\`json
${STATUS_JSON:-no data}
\`\`\`
REPORT_EOF
