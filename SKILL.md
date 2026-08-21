---
name: codexcleaner
description: "Audit and clean local Codex task history on Windows: classify tasks to keep, archive, or permanently delete; execute confirmed cleanup; and diagnose stale sidebar entries such as 'no rollout found'. Use when Codex task history is large, sluggish, cluttered, or inconsistent."
---

# Codex Cleaner

Clean Codex task history while preserving the user's projects and chosen work.

## Select the language

- For a Chinese-speaking user, read [references/workflow.zh-CN.md](references/workflow.zh-CN.md).
- For an English-speaking user, read [references/workflow.en.md](references/workflow.en.md).
- Read one workflow unless the user explicitly requests both languages.

## Operating boundaries

- Begin with a read-only audit and present a task-by-task plan.
- Prefer Codex thread tools when the host exposes them. Otherwise use the official `codex archive`, `codex unarchive`, and `codex delete` CLI commands.
- Treat archive as recoverable organization: the task leaves the active left sidebar while its stored history remains available for unarchive.
- Treat delete as permanent removal: the stored task and its spawned descendants are deleted and cannot be recovered through Codex.
- Keep project directories, repositories, source code, documents, and build artifacts outside the cleanup scope.
- Obtain explicit confirmation immediately before permanent deletion. Name every selected top-level task and summarize the count and known storage size.
- Use official operations first. Reserve direct database repair for verified stale metadata, a separate confirmation, an integrity-checked backup, and an app-closed maintenance window.
- After every mutation, verify the kept tasks, archived tasks, rollout files, and desktop sidebar catalog.

## Read-only audit helper

Run `python scripts/audit_codex.py` when a Windows-local inventory or stale-entry diagnosis is useful. It reads task metadata and file sizes without reading conversation bodies or changing Codex data.
