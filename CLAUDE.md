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

## Releases

- Bump via a dedicated `chore(release): bump version to X.Y.Z` PR whose body lists every PR merged since the previous bump — the PR body is the changelog; fixes/features never bump the version inside their own PR
- 0.x semver: breaking `feat!` since the last release → minor bump; only `feat`/`fix` → patch
- Single version source: `__version__` in `src/dicorina/__init__.py` (hatch dynamic versioning) — a bump edits only that line; `uv.lock` records the project version as dynamic, no relock needed
- After the release PR merges: `git tag vX.Y.Z <merge-commit>` + `git push origin vX.Y.Z` (optionally `gh release create vX.Y.Z` reusing the PR body)
