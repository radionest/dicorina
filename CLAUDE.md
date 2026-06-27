# dicorina

## Worktree Workflow

- Feature development: always enter a worktree via `EnterWorktree` before making changes
- Any file change needs a worktree — the `require-worktree` hook blocks Edit/Write on `main`; only `.claude/` infrastructure files are exempt
- Worktrees contain only git-tracked files. `hooks/`, `settings.json`, `settings.local.json` live in `$CLAUDE_PROJECT_DIR/.claude/` and are shared
- `ExitWorktree(remove)` requires `discard_changes=true` if there are commits not in main
- For PRs in review prefer `ExitWorktree(keep)` until merge
- The Stop hook blocks session end in a worktree — ask the user to choose:
  1. **Push + PR**: commit all → `git push -u origin <branch>` → `gh pr create` → `ExitWorktree(keep)`
  2. **Keep**: `ExitWorktree(keep)` — worktree stays for later
  3. **Discard**: `ExitWorktree(remove, discard_changes=true)`
