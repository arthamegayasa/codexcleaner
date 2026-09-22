---
name: codexcleaner
description: "Audit Windows storage used by AI tools and prepare reviewed, recoverable cache cleanup while preserving projects, task history, application state, and generated outputs. Use for disk-space investigations or explicitly requested storage cleanup. Task-history management is a separate mode only when requested."
---

# CodexCleaner

Find the real storage consumers before proposing changes. This fork's storage workflow is authoritative over the retained upstream cleanup references.

## Audit

Run `scripts/storage_audit.py --only-roots` with explicit `--root` paths for a scoped audit; omit `--only-roots` only when default application discovery is within the requested scope. Consult `--help` for options. Save reports under an existing parent, outside inspected targets, using new names. It inspects metadata without reading conversations or document bodies.

Report the largest non-overlapping folders, useful deeper groups, inaccessible paths, skipped links, and scan limitations. Check completeness/limit markers; do not describe a bounded partial scan as complete. Label logical per-path sizes; hardlinks, sparse virtual disks, and application aliases can make them differ from occupied or recoverable space. Do not add a parent and its descendants into one savings estimate.

Separate confirmed rebuildable caches, items requiring review, and protected work/state. Projects, repositories, worktrees, databases, task history, authentication/browser state, reports, exported images, and archives are not generic cache. A Temp location, age, or naming prefix does not establish disposability. In particular, NeuroGuide report/research/test artifacts remain REVIEW until their exact role and retained outputs are established.

For an audit-only request, finish with findings and proposed next steps; do not run a mutation.

## Reviewed cleanup

Before preparing or executing a cleanup plan, read [references/storage-safety.md](references/storage-safety.md). It defines policy scope, protection, age, revalidation, process blocking, quarantine, restore, and purge.

Use `scripts/storage_guard.py` for this workflow. Start with empty rules, add the user's protected project/output roots, and configure only exact reviewed disposable directories with reasons. Present the actual plan and its ID. Reuse existing explicit authorization when it covers that concrete scope; obtain missing authorization before changing files.

Complete active work before mutations. Relevant AI/tool processes must be closed normally, and process inspection must succeed. Use an external terminal; report blockers without force-closing processes or bypassing checks. Skip changed files and report the result rather than broadening scope.

Keep the entire quarantine run directory in place: manifest, journal, and recovery files. The journal supports interrupted-run recovery, but the operation is not an atomic filesystem transaction; report partial results without promising uninterrupted active work. Same-volume quarantine does not release disk space. Verify the application's operation and retained outputs, then consider purge only as a separately authorized operation after the retention period. Never substitute deletion of original sources for manifest-based purge or schedule recurring cleanup from a one-time request.

Completion means every planned item has a reported result, skipped or blocked items are explained, and the manifest/restore instructions are available. Report measured space changes with timing caveats rather than claiming the plan's logical total was freed.

## Explicit task-history requests

Use `scripts/audit_codex.py` for metadata-only task auditing. Prefer supported official task tools available in the current environment for separately authorized task changes. Treat task history and sidebar organization separately from storage caches. Preserve archive/recovery semantics and require explicit authorization for the exact permanently deleted tasks.

The older bilingual workflow/cache references and `cache_maintenance.py` are retained upstream material, not the recommended storage mutation path. Direct database editing or repair is outside this skill's recommended workflow.

## User-facing guides

- [README.md](README.md): English setup and command examples.
- [README.id.md](README.id.md): Indonesian quickstart.
- [README.zh-CN.md](README.zh-CN.md): original upstream Chinese material; use the fork's current storage workflow above for cleanup.
