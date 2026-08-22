# CodexCleaner workflow (English)

## Goal

Help the user turn the Codex left sidebar into a clear, workable task set while fully preserving project directories, source code, and every task the user chooses to keep.

This file covers task history and sidebar state only. When the user reports slow startup, focus-switch freezes, long cursor spins, or “Not Responding,” also read [cache-maintenance.en.md](cache-maintenance.en.md) and audit rebuildable caches. A clean task list does not imply clean performance caches.

## Three outcomes

### Keep

A kept task stays in the active task list. Choose this for work in progress, work that will resume soon, unique decision context, or anything the user explicitly protects.

### Archive

Archive is the default organization choice. Archiving moves the task log into Codex's archived storage. The task leaves the active left sidebar, its history remains stored, and unarchive can restore it.

Prefer archive for tasks that are:

- complete and still useful as references;
- paused with a realistic chance of resuming;
- valuable for requirements, decisions, investigation history, or handoff context;
- still uncertain from the user's point of view.

Archiving provides a clean active list and recoverable history at the same time. With a large task set, it can also reduce the active data the desktop client processes during startup and sidebar rendering.

### Permanently delete

Permanent deletion is for tasks the user clearly no longer needs. Codex deletes the persisted task and its spawned descendants; the task leaves the sidebar and its history is no longer recoverable. Project directories, Git repositories, source code, and ordinary files remain in place.

Good permanent-deletion candidates include:

- abandoned experiments, tests, or empty tasks;
- duplicates with a more complete surviving version;
- one-time investigations whose conclusions now live in documentation or code;
- tasks for retired projects with no remaining reference value;
- broken records whose underlying conversation file is already absent.

## Workflow

### 1. Read-only audit

Prefer host-provided active and archived task-list tools. When Windows-local storage or sidebar consistency needs inspection, run this from the skill root:

```powershell
python scripts/audit_codex.py
```

For machine-readable output:

```powershell
python scripts/audit_codex.py --json
```

The helper reads task metadata, parent-child relationships, rollout existence, and file sizes. It does not read conversation bodies or change either database.

Also run `codex doctor --summary` and record the Codex version and storage diagnostics. Keep the report local by default; review machine paths and task titles before sharing it.

When the primary goal is responsiveness, run the read-only cache audit too. Report task-history size and performance-cache size separately; do not collapse them into a single “fully cleaned” conclusion.

### 2. Recommend outcomes

Apply these rules in order:

1. Keep the current task, pinned tasks, explicitly protected tasks, and work the user plans to continue soon.
2. Archive completed work that still supports reuse, traceability, or a possible future continuation.
3. Recommend permanent deletion for clearly unwanted, duplicate, test, empty, or unrecoverably broken tasks.
4. Place uncertain tasks in Archive or Needs decision so their history stays recoverable.
5. Treat spawned agent tasks as part of their top-level task. List the top-level task in the plan and state that deletion includes descendants.

The recommendation table should include action, title, task ID, rollout size, last update, reason, and visible result. Summarize the task count and known storage size for Keep, Archive, and Delete separately.

### 3. Obtain confirmation

Before archiving, show the complete archive list and receive confirmation for that list.

Before permanent deletion, use a clear affirmative confirmation such as:

```text
I confirm permanent deletion of these N Codex tasks and their stored conversation data; keep ...; leave all project directories and code in place; I understand the deleted task history cannot be recovered.
```

The confirmation covers the exact displayed tasks. When the list changes, display the revised list and obtain a fresh confirmation. Count Archive and Permanent delete separately so every action remains clear.

### 4. Use official operations

Prefer the desktop task tools when they are available. Otherwise use UUIDs with the CLI:

```powershell
codex archive <TASK_UUID>
codex unarchive <TASK_UUID>
codex delete <TASK_UUID> --force
```

Use `--force` only for UUIDs covered by explicit confirmation. Record each result. When an operation reports an error, verify the resulting state before choosing the next action; this detects partial completion accurately.

### 5. Verify

Run the audit again and verify that:

- every kept task remains active with an existing rollout;
- every archived task is archived and can be unarchived;
- every permanently deleted task is absent from the official task store;
- the active sidebar contains the expected tasks;
- SQLite integrity checks return `ok`;
- project directories and code remain in place.

Report the actual kept, archived, and permanently deleted counts, released storage, and any remaining inconsistency.

## Interpreting sidebar inconsistencies

The desktop sidebar can maintain a local catalog. Interpret audit findings as follows:

- `ghost_catalog_entries`: the sidebar catalog still references a task that is absent from the official state database. This is a removable ghost index entry.
- `stale_archived_catalog_entries`: the task is archived while the active sidebar catalog still retains its old entry. This is a refreshable or removable archived index entry.
- `state_rows_missing_rollout`: task metadata remains while the actual conversation log is absent. Normal resume cannot succeed, and the outcome of the official delete operation needs verification.

For “Failed to resume conversation” or `no rollout found`, complete this diagnosis first. An index repair cannot recreate a missing conversation log; it restores agreement between the visible list and the data that actually exists.

## Database repair boundary

Treat database repair as a separate maintenance action with all of these conditions:

1. Official archive or delete operations have run and their results have been verified.
2. Each target is an archived stale index, a ghost absent from the state database, or orphan metadata whose rollout is already absent.
3. The user separately confirms the exact target IDs.
4. The Codex desktop app is fully closed.
5. Each database receives a timestamped online backup and passes `PRAGMA quick_check` first.
6. One transaction removes only metadata associated with confirmed IDs.
7. Integrity checks run again before Codex restarts.
8. Backups remain available until the user verifies the restarted app.

When the schema is unfamiliar, a writer is active, a database is locked, or the target set changes, preserve the current data and report the diagnosis. Repair scope stays limited to Codex task indexes and orphan metadata.
