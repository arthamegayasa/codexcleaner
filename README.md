# CodexCleaner

[简体中文](README.zh-CN.md)

> Is Codex on Windows getting slower over time? Long startup spins, UI freezes, failed task restores, and “ghost” entries stuck in the sidebar?  
> **CodexCleaner is built to audit and clean up local Codex task history.**

## Why This Skill Exists

The problem was simple:

**Codex worked fine one day, then suddenly became painfully slow the next.**

On the Windows Codex desktop app, we repeatedly ran into issues like:

- noticeably slower startup
- the cursor spinning for a long time
- sluggish UI or full “Not Responding” states
- no obvious CPU spike
- no corresponding increase in fan activity

At first, we assumed it was just a one-time initialization after an update.

Then it happened again.

So we started digging.

We also found many similar reports online: slow startup, frozen UI, failed task recovery, stale sidebar entries, and `no rollout found`.

The exact environment and root cause vary from case to case, but one area was clearly worth inspecting:

**Codex task history, session storage, and sidebar indexes on Windows.**

We then ran two rounds of cleanup on our own Codex installation.

### Round One

We permanently deleted 13 large tasks that were no longer needed, freeing about **338.60 MB** of session data.

After restarting Codex, responsiveness improved.

### Round Two

We ran a full audit:

- **145 task records** checked
- **131 active records**
- multiple oversized sessions found
- a large number of hidden Guardian / derived tasks discovered

Final result:

- ✅ **6** active tasks kept
- 📦 **16** useful tasks archived
- 🗑️ **123** unnecessary records permanently deleted
- 🧹 stale task indexes and ghost sidebar entries cleaned up

After another restart, the active task list was much smaller, broken entries were gone, and task browsing remained smooth.

From those two rounds of cleanup, we turned the working criteria and operation order into a reusable workflow.

That became **CodexCleaner**.

It focuses on local issues that can actually be inspected and handled:

- bloated task history
- oversized sessions
- hidden Guardian / derived records
- sidebar index inconsistencies after official archive or delete operations
- `no rollout found` cases related to mismatched history and cached indexes

System configuration, networking, client-version issues, and other external factors should still be checked with normal Codex diagnostics.

**CodexCleaner has one clear job: audit, classify, confirm, clean, and verify.**

It first generates a clear **Keep / Archive / Permanently Delete** plan. After the user confirms the actions, it uses official Codex task operations and then verifies the resulting local storage state and sidebar list.

## What Happens to the Sidebar

| Action | Sidebar Result | Task History | Best For |
| --- | --- | --- | --- |
| Keep | Remains in the active list | Fully preserved | Current or important work |
| Archive | Removed from the active list | Fully preserved and can be restored | Finished or paused work that may still be useful |
| Permanently Delete | Removed | Permanently deleted, including derived tasks | Tasks that are clearly no longer needed |
| Repair Stale Index | Broken or stale entries are removed | Realigned with the history that actually exists | `no rollout found` and cache inconsistencies |

### Cleanup Scope

**Project directories, Git repositories, source code, documents, and build artifacts are always outside CodexCleaner’s cleanup scope.**

By default, **archive is the recommended option**.

It keeps the active list focused while preserving the context you may want to continue or revisit later.

**Permanent deletion should only be used for tasks that have been explicitly reviewed and confirmed as no longer valuable.**

## Classification Rules

- **Keep:** Current work, pinned tasks, unique context, or tasks you expect to continue soon.
- **Archive:** Finished or paused tasks that still have reference, traceability, or future continuation value.
- **Permanently Delete:** Tests, empty tasks, duplicates, finished one-off work, or broken records whose session files no longer exist.
- **Undecided:** Anything that cannot be classified confidently stays recoverable until the user decides.

In short:

**Archive whenever possible. Permanently delete only after explicit confirmation.**

## Installation

Clone or copy this directory into your user Skill directory:

```powershell
git clone <repository-url> "$env:USERPROFILE\.codex\skills\codexcleaner"
```

Codex will usually detect Skill changes automatically.

If the new Skill does not appear, restart Codex once.

## Usage

Explicit invocation:

```text
$codexcleaner Audit my Codex tasks and suggest which ones to keep, archive, or permanently delete. Show me the full plan before making any changes.
```

Default workflow:

1. Run a read-only audit.
2. Review tasks individually and summarize task count and known size.
3. Confirm the archive list and permanent-delete list separately.
4. Use official Codex archive and delete operations.
5. Verify storage state and the sidebar list afterward.

**No destructive action should happen before user confirmation.**

## Read-Only Audit Tool

The project includes a read-only audit script that uses only the Python standard library:

```powershell
python scripts/audit_codex.py
python scripts/audit_codex.py --json
```

The script reads task metadata and file sizes only.

**It does not read conversation bodies and does not modify Codex data.**

For custom database paths or protected task IDs:

```powershell
python scripts/audit_codex.py --help
```

## Environment and Scope

- Windows 10 or Windows 11
- A current Codex CLI version with `doctor`, `archive`, `unarchive`, and `delete`
- Python 3.9+ for the optional audit script

Codex task storage is an implementation detail that may evolve over time.

For that reason, CodexCleaner prioritizes official task operations whenever possible.

Direct database repair is treated as a separate maintenance workflow that requires explicit confirmation, backups, and integrity checks.

For official semantics, see [Codex App Server API Overview](https://learn.chatgpt.com/docs/app-server#api-overview).
