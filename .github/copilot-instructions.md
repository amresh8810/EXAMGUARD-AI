# ExamGuard AI workspace rules

Read [FEATURES.md](../docs/FEATURES.md) before changing application code. It
records the existing features and their main implementation files.

## Protect existing features

- Treat every feature in `docs/FEATURES.md` as existing, user-approved behavior.
- Do not change, remove, replace, or refactor an existing feature as a side
  effect of adding an unrelated feature.
- Before editing code that implements or can affect an existing feature, explain
  why the change is necessary, name the affected behavior, and ask the user for
  explicit approval. Do not proceed until the user says yes.
- This approval rule also applies to shared routes, authentication/session
  handling, exam state, monitoring, database schemas, and shared frontend code
  when a change could alter an existing feature.
- If the user does not approve, stop at the boundary and offer an isolated
  alternative that does not alter existing behavior.

## Add features in isolation

- Make the smallest complete change. Prefer a new module/service, route, template,
  or feature-specific asset over rewriting a large existing file.
- If a shared-file integration point is unavoidable, keep it narrowly additive
  and preserve all existing behavior. Ask first if that integration could affect
  an existing feature.
- Do not perform opportunistic cleanup, broad formatting, renaming, or
  restructuring while implementing a new feature.
- Preserve existing API contracts, database records, and user changes. Never
  overwrite or delete `.env`, candidate face data, exam records, or other
  existing user data unless the user explicitly requests it.
- Update `docs/FEATURES.md` when an approved change adds or changes a feature.

## Validate safely

- Inspect the worktree and relevant code before editing; do not revert changes
  that predate the task.
- Run targeted validation for the new feature and relevant regression checks.
- If validation finds a defect in a protected existing feature, report it and
  ask before changing that feature.
- Report clearly which files changed, which existing features were left
  untouched, and what validation was run.
