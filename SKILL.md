---
name: codexcleaner
description: "Restore Codex responsiveness on Windows by auditing and safely cleaning two separate layers: task history/sidebar state and rebuildable desktop caches. Use for slow startup, focus-switch freezes, 'Not Responding', bloated task history, stale sidebar entries, or 'no rollout found'."
---

# Codex Cleaner

Restore Codex responsiveness while preserving projects, chosen tasks, login state, and user data.

## Select the mode and language

- For task-history cleanup in Chinese, read [references/workflow.zh-CN.md](references/workflow.zh-CN.md); for English, read [references/workflow.en.md](references/workflow.en.md).
- For performance-cache diagnosis or cleanup in Chinese, read [references/cache-maintenance.zh-CN.md](references/cache-maintenance.zh-CN.md); for English, read [references/cache-maintenance.en.md](references/cache-maintenance.en.md).
- When the user asks for a complete cleanup or reports performance symptoms without a known cause, read both references in the user's language and audit both layers. Do not assume task count and cache state are interchangeable.

## Operating boundaries

- Begin with a read-only audit and present a task-by-task plan.
- Begin performance work with a read-only cache audit. Do not clear caches merely because the skill was invoked.
- Prefer Codex thread tools when the host exposes them. Otherwise use the official `codex archive`, `codex unarchive`, and `codex delete` CLI commands.
- Treat archive as recoverable organization: the task leaves the active left sidebar while its stored history remains available for unarchive.
- Treat delete as permanent removal: the stored task and its spawned descendants are deleted and cannot be recovered through Codex.
- Keep project directories, repositories, source code, documents, and build artifacts outside the cleanup scope.
- Keep cookies, authentication storage, Local Storage, IndexedDB, Session Storage, and all `$CODEX_HOME` task/session data outside cache cleanup.
- Obtain explicit confirmation immediately before permanent deletion. Name every selected top-level task and summarize the count and known storage size.
- Obtain explicit confirmation immediately before cache cleanup. Require a fully closed desktop app, move only the allowlisted rebuildable caches into a timestamped backup, and never run the mutating helper as an ordinary child process that will die with Codex.
- Use official operations first. Reserve direct database repair for verified stale metadata, a separate confirmation, an integrity-checked backup, and an app-closed maintenance window.
- After every mutation, verify the relevant layer. Cache cleanup additionally requires checking login/task preservation and testing at least three browser-to-Codex focus switches.
- Do not schedule periodic cache deletion. Repeat it only when symptoms recur or an audit supports it.

## Read-only audit helper

Run `python scripts/audit_codex.py` when a Windows-local inventory or stale-entry diagnosis is useful. It reads task metadata and file sizes without reading conversation bodies or changing Codex data.

Run `python scripts/cache_maintenance.py audit` for a read-only cache inventory. For confirmed cleanup, use the app-closed procedure in the cache-maintenance reference; `scripts/run_cache_cleanup.cmd` is the user-visible fallback.
