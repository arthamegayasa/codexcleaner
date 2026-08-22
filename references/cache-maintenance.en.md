# CodexCleaner performance-cache maintenance (English)

## Goal and trigger

Diagnose and clear rebuildable browser caches used by the Windows Codex desktop app when symptoms include slow startup, freezes after switching back from another window, long cursor spins, input-method stalls, or brief “Not Responding” states.

Cache maintenance and task-history cleanup are separate layers. A small task list or a completed archive/delete pass does not prove that Chromium, Service Worker, code, or GPU caches are healthy.

Do not use cache cleanup as scheduled maintenance. Run it only when relevant symptoms recur or a read-only audit shows unusually large or long-lived caches.

## Read-only audit

Run from the skill root:

```powershell
python scripts/cache_maintenance.py audit
python scripts/cache_maintenance.py audit --json
```

The report separates GPU, HTTP, code, Service Worker, and component caches and inventories existing backups. It does not modify files.

For a performance diagnosis, also record:

- the Codex app version;
- whether symptoms occur at startup, task restore, or focus switching;
- total cache size and the largest cache directories;
- the task-history audit result;
- subjective behavior before and after cleanup plus three focus-switch trials.

Do not infer the root cause from byte count alone. Use cache size, directory age, and reproducible symptoms together.

## Safety boundary

The helper uses a fixed allowlist and moves only these rebuildable categories:

- Chromium HTTP Cache and Code Cache;
- GPUCache, ShaderCache, and Dawn/WebGPU/Graphite caches;
- Service Worker data in the `codex-browser-app` partition;
- Chromium component and extension-package caches.

Always preserve:

- cookies, login, and authentication state;
- Local Storage, IndexedDB, Session Storage, and WebStorage;
- Network directories and other partition site state;
- active tasks, archived tasks, rollout sessions, and databases under `$CODEX_HOME`;
- project directories, Git repositories, source code, documents, and build artifacts.

The helper moves caches into `%LOCALAPPDATA%\OpenAI\Codex-cache-backups\<timestamp>` instead of deleting them and writes `manifest.json`. Keep the backup until the user confirms login, tasks, and responsiveness are intact; normally request separate confirmation before removing it after 24–48 stable hours.

## Confirmation

Before cleanup, show:

- the target cache directories and total size;
- the protected data categories;
- the backup location;
- the requirement to fully exit Codex;
- that the first restart may be slightly slower while caches rebuild.

Confirmation covers the current audit result. Re-audit and reconfirm if the target set or size changes materially.

## Execute only after the app is closed

Windows can terminate ordinary child processes when Codex exits, so a background child is not proof that maintenance is armed. Use one of these methods:

1. Have the user fully exit Codex from the system tray, then run in an external terminal:

   ```powershell
   python scripts/cache_maintenance.py clean --apply --relaunch
   ```

2. Use the visible `scripts\run_cache_cleanup.cmd`. It refuses to clean while `ChatGPT.exe` is running; after the user exits the app, run it again.

Use `--wait-seconds` only from an independently hosted Windows task when the host supports it and the user authorizes it. Verify that the task is actually running and its independent process exists; if the task captures a status log, verify the waiting stage there too. Remove the exact one-time task after completion. Do not rely on a normal process spawned by Codex itself.

Stop and preserve remaining data when the app is still running, files are locked, a path escapes the profile, a target is a symbolic link, or the backup directory overlaps the profile.

## Verification

After cleanup, verify that:

1. `manifest.json` reports `completed` with no failures;
2. login remains valid and active/archive task counts did not change unexpectedly;
3. protected directories still exist and project files are unchanged;
4. the app recreates cache directories at a much smaller size;
5. at least three “Codex → browser interaction → Codex” focus switches complete normally;
6. the final report includes actual moved size, backup location, and subjective result.

If the problem returns within days, do not keep clearing caches. Investigate the client version, graphics stack, app logs, and a reproducible defect while preserving the latest audit and cleanup manifest as evidence.
