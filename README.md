# CodexCleaner — storage audit and recovery

[Bahasa Indonesia](README.id.md) · [Original upstream Chinese guide](README.zh-CN.md)

Audit Windows storage used by AI tools, distinguish caches from valuable work, and quarantine only reviewed disposable files. A large folder, an old timestamp, or a name containing `Temp` is not evidence that its contents can be discarded.

> **Fork guidance:** this fork extends [Kynepen/codexcleaner](https://github.com/Kynepen/codexcleaner). For storage cleanup, this README, [SKILL.md](SKILL.md), and [storage-safety.md](references/storage-safety.md) are authoritative. The original Chinese documentation and older references are retained as upstream material. Task-history deletion is a separate, explicitly requested operation; it is not part of the storage cleanup workflow.

## Audit first

Requires Python 3.10 or later, with no additional Python packages. Audit and planning leave inspected files unchanged; apply, restore, and purge are Windows-only.

From this repository, run:

```powershell
New-Item -ItemType Directory -Path work -Force | Out-Null
python scripts/storage_audit.py --only-roots --root "$env:LOCALAPPDATA" --json --output "work/storage-audit.json" --markdown "work/storage-audit.md"
```

Repeat `--root` for another location. `--only-roots` limits the scan to those locations; without it, default discovery adds known application locations. Choose fresh report names for each run: existing reports are not overwritten. Reports contain local paths, so keep them private. The audit reads filesystem metadata, reports inaccessible locations and skipped links, and does not read conversation bodies or document contents.

Check the report's completeness and limit markers. The scan is bounded by time, entry count, directory count, and depth; an incomplete scan exits with status 1. Use narrower roots or adjust the limits shown by `--help` before treating an incomplete result as a full audit.

Folder totals are **logical bytes per path**, not guaranteed recoverable disk space. Hardlinks, application virtualization aliases, sparse files, and nested totals can distort a naive sum. Two visible Codex profile paths can refer to the same directory. A `.vhdx` file can contain an entire working environment.

## Preserve work and state

Projects, repositories, worktrees, source code, documents, databases, reports, exported images, archives, task history, credentials, and browser state are valuable data until reviewed otherwise. `LocalCache` can hold application state. Temp can hold the only copy of a report or backup.

Repeated `neuroguide-report-*` or `neuroguide-research-*` folders are **review items**, not a deletion pattern. Review the exact directory, retained outputs, and active work before selecting a disposable subset. Built-in project/state markers add protection but cannot determine every file's value; configure protected roots for your own work.

## Plan, quarantine, restore, then purge separately

Read the [policy and safety reference](references/storage-safety.md) before planning cleanup. Save this empty policy as `policy.json`:

```json
{
  "version": 1,
  "protected_roots": [],
  "rules": []
}
```

Add project/output locations to `protected_roots`. Add a rule only for one exact reviewed disposable directory, with a reason. There is no default rule to empty Temp or an AI application profile.

```powershell
python scripts/storage_guard.py plan --policy policy.json --output plan.json
```

Review the selected files, skipped/protected entries, sizes, and printed plan ID. Use a fresh output name when making another plan. Empty rules select nothing. The minimum file age is **seven days**. Plans expire after **24 hours**; apply revalidates the selection and skips changed files. A `reviewed_temp` directory is excluded as a whole when preflight detects changed or ineligible contents.

Audit uses metadata only. Planning reads candidate file bytes for protection checks and SHA-256 fingerprints; quarantine and recovery verify content integrity. The tools run locally without an API key or network upload.

After the exact scope is authorized, finish active work and close the relevant AI tools normally. Run mutations from an **external PowerShell terminal**. The guard refuses mutations when relevant AI/tool processes are running or process inspection fails. It never force-closes applications.

```powershell
python scripts/storage_guard.py apply --plan plan.json --policy policy.json --quarantine-root "D:\CodexCleaner-Quarantine" --confirm PLAN_ID
```

Replace `PLAN_ID` with the printed ID and choose a real quarantine volume with sufficient space. Quarantine must be outside the source and protected roots. A verified copy to another volume can release source-volume space while retaining recovery data. Quarantine on the same volume **does not free space** on that volume.

Keep the **entire quarantine run directory in its original location**, including `manifest.json`, `journal.jsonl`, and `files`. The journal records progress before removal and supports recovery after interruption. For reviewed Temp bundles, the guard locks and verifies copies of all selected members before removing any source. This is not an atomic filesystem transaction: a crash or concurrent activity can still leave a partial run. Preserve verified copies and inspect the recorded results.

This is a recovery path for disposable files, not a complete backup: original NTFS permissions/ACLs are not backed up. To restore, use the run ID recorded in the manifest:

```powershell
python scripts/storage_guard.py restore --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Verify normal operation and required outputs. Permanent removal requires a **separate decision** and at least **seven days of quarantine retention**:

```powershell
python scripts/storage_guard.py purge --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Purge targets recorded quarantine files, never original source paths. Keep the manifest as an audit trail. See the [reference](references/storage-safety.md) for process blockers, interrupted runs, and restore conflicts. This workflow does not schedule automatic cleanup.

## Use as a Codex skill

Install the feature branch explicitly while the storage guard changes are awaiting merge:

```powershell
git clone --branch feature/windows-storage-guard https://github.com/arthamegayasa/codexcleaner.git "$env:USERPROFILE\.codex\skills\codexcleaner"
```

```text
$codexcleaner Audit the folders consuming space on C. Separate disposable caches from projects, history, application state, and generated outputs. Show findings before proposing changes.
```

## Task history and older helpers

The upstream metadata-only task audit remains available:

```powershell
python scripts/audit_codex.py --json
```

A request to free disk space does not authorize removing task history. For separately requested task management, use supported official task operations available in the current environment. Direct database editing or repair is outside the recommended workflow.

The older `cache_maintenance.py`, launcher, and references remain for upstream continuity. The launcher audits by default and requires `--apply` for changes. The legacy helper preserves all Service Worker data and moves allowed caches only by same-volume rename; it rejects cross-volume backups, frees no space through that move, and never automatically purges backups.

Use `storage_guard.py` for this fork's reviewed storage cleanup, including verified cross-volume quarantine. Its policy, run directory, journal, and retention workflow are separate from legacy cache backups; their manifests are not interchangeable. See the [updated English legacy reference](references/cache-maintenance.en.md) only when that helper is specifically needed.

## Development

```powershell
python -m unittest discover -s tests -v
```

CI runs unittest on Windows and Ubuntu with Python 3.10 and 3.13. Tests use temporary fixtures, not a developer's real profile. Windows remains the supported mutation platform.

## Attribution

Upstream: [Kynepen/codexcleaner](https://github.com/Kynepen/codexcleaner). Fork: [arthamegayasa/codexcleaner](https://github.com/arthamegayasa/codexcleaner). Upstream history and the original Chinese documentation are preserved. The imported upstream snapshot did not include a `LICENSE` file; this fork does not introduce or infer a license grant.
