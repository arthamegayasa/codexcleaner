# Reviewed storage cleanup

Read this before creating a cleanup policy or running `storage_guard.py`. It is the authoritative storage workflow for this fork. Older task-history and cache helpers are separate upstream material.

## Classify before selecting

| Finding | Treatment |
| --- | --- |
| Rebuildable cache with a known owner and exact boundary | Consider an exact `cache` rule after reviewing regeneration requirements. |
| Completed temporary run with useful outputs retained elsewhere | Consider an exact `reviewed_temp` rule with a reason recording that review. |
| Temp report, research export, database copy, backup, or unknown generated artifact | REVIEW: establish its role, retained outputs, and active work. |
| Project, repository, worktree, source, retained report/image/archive, task history, or state database | Protect it; storage pressure does not make it disposable. |
| Browser profile, cookies, credentials, Local Storage, IndexedDB, or session state | Protect it; a parent named `LocalCache` is not sufficient evidence of cache. |
| `.vhdx` or another virtual disk | Treat as an environment/data container, not a cleanup target inferred from size. |

Recognized project/state markers provide an additional guard, not a complete classifier. Protect important roots explicitly. Generic file names and ordinary extensions do not prove replaceability.

`neuroguide-report-*`, `neuroguide-research-*`, and similar Temp names are grouping aids only. Never convert a prefix or old timestamp into blanket deletion. Review individual directories and keep unique results or backups outside any proposed disposable boundary.

## Policy

Policy paths are absolute, exact directory paths, not wildcard patterns. Resolve them against the intended volume before mutation. Start with:

```json
{
  "version": 1,
  "protected_roots": [],
  "rules": []
}
```

Empty rules select nothing. Add protected project/output roots first, then exact confirmed disposable roots. A rule has `root`, `kind`, `reason`, and `min_age_days`:

```json
{
  "version": 1,
  "protected_roots": [
    "C:\\Users\\Example\\Documents\\Projects",
    "D:\\Completed-Outputs"
  ],
  "rules": [
    {
      "root": "C:\\Users\\Example\\AppData\\Local\\Temp\\demo-tool-run-12345",
      "kind": "reviewed_temp",
      "reason": "Reviewed this completed demo run; useful outputs are retained outside this directory.",
      "min_age_days": 7
    }
  ]
}
```

This fictional example is not a default target. Replace paths only after reviewing the real directory. Use `cache` for a confirmed rebuildable cache and `reviewed_temp` for a reviewed disposable temporary run. Neither kind authorizes valuable data inside a broad parent.

The minimum age is seven days; increasing it is allowed. Age is a filter after classification, not evidence of disposability. Protected roots and recognized project/state boundaries override a broad rule.

The guard also rejects links/reparse paths, hardlinked files, alternate data streams, and special file attributes such as sparse or compressed storage. Unlike the metadata-only audit, planning reads candidate bytes for database signatures and SHA-256 content fingerprints; quarantine/recovery also read bytes to verify integrity. No API key or network upload is involved. A `reviewed_temp` rule treats the directory as one reviewed unit: an ineligible member or changed membership detected during preflight excludes the whole directory.

## Plan and authorization

```powershell
python scripts/storage_guard.py plan --policy policy.json --output plan.json
```

Planning changes no source files. Use a fresh plan output path; existing outputs are not overwritten. Review exact selected paths, reasons, ages, and skipped/protected entries. The printed plan ID identifies the reviewed selection. Plans expire after 24 hours; regenerate and review an expired plan instead of editing its timestamp or ID.

Keep policy, plan, reports, and manifests outside cleanup targets. These files may expose private paths; do not commit local copies.

Authorization must cover the concrete action and scope. An audit request is not cleanup authorization. Reuse existing explicit authorization that covers the reviewed scope rather than introducing a second approval ritual. Purge is a separate permanent action from recoverable quarantine.

## Apply and process checks

Apply, restore, and purge are Windows-only. Run them from an external terminal after work finishes and relevant applications close normally. The guard blocks relevant AI/tool processes, including Codex, ChatGPT, Claude, Node, and Python other than its own process. It also blocks if process inspection fails. Do not force-close programs or bypass this check.

```powershell
python scripts/storage_guard.py apply --plan plan.json --policy policy.json --quarantine-root "D:\CodexCleaner-Quarantine" --confirm PLAN_ID
```

Use the printed plan ID. Quarantine must be outside source/protected roots, with sufficient destination space. Apply revalidates the policy, plan age, file metadata, protection, and process state. Changed items are skipped; newly discovered files do not silently replace the selection.

The copy is hash-verified before its source is removed through the held Windows handle. For a reviewed Temp bundle, all selected members are locked and copied with verification before the first source removal. The tool checks membership again and records progress in a write-ahead journal with a hash chain. These safeguards do not make the operation an atomic filesystem transaction against crashes or concurrent writers; partial outcomes remain possible, with verified recovery copies retained.

Keep the entire quarantine run directory at its original path: `manifest.json`, `journal.jsonl`, and `files`. Do not move or edit those files independently. Original NTFS permissions/ACLs are not backed up; this is a recovery mechanism for disposable files, not a complete backup of important data. Cross-volume quarantine can release source-volume space while retaining recovery data. Same-volume quarantine does not free space on that volume. Logical selected bytes do not guarantee an equal change in free space, especially with hardlinks or concurrent application activity.

If interrupted or failed, preserve the manifest, journal, recovery files, and remaining sources before further action. The presence of a destination directory is not proof of completion. Restore replays valid journal records to recover progress beyond the last manifest checkpoint. An inconsistent or incomplete journal blocks automated recovery; retain all copies for examination rather than editing the journal or blindly repeating apply.

## Restore

```powershell
python scripts/storage_guard.py restore --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Use the manifest and run ID from the run, with its journal beside it. Restore checks the recorded copy and destination, verifies a temporary copy before publishing it at the original path, and must not overwrite a newly created original path. When a conflict, changed copy, missing file, or active process blocks restoration, retain the whole run directory and report the blocker. Resolve the specific conflict with the user rather than forcing an overwrite.

Verify returned files and application behavior. Keep the manifest as a record of restored, skipped, and failed entries. It cannot recreate data later deleted or modified outside the tool.

## Retention and purge

```powershell
python scripts/storage_guard.py purge --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Purge permanently removes eligible recorded quarantine files after at least seven days of retention, with separate authorization and process/integrity checks. It never targets original source paths. Do not shorten retention, edit the manifest, or use another deletion command to bypass a blocked purge.

Verify required outputs remain available and restoration is no longer needed. Retain the manifest/result for the audit trail. One cleanup request does not authorize scheduled cleanup or recurring purge.

## Completion report

Summarize the inspected scope and metric, reviewed selection, every item's result, blockers, and manifest location. Include the restore command after quarantine. Record before/after times when measuring volume free space; distinguish that observed change from logical file totals. Keep uncertain items in REVIEW.
