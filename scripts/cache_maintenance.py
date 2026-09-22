#!/usr/bin/env python3
"""Audit and safely move rebuildable Codex desktop caches on Windows.

The cleanup is allowlist-based. Known authentication/state stores and Service
Worker data are excluded; unexpected database or state files block the run.
Cache directories are renamed into a timestamped backup on the same volume,
without deleting backups or claiming that moved bytes are reclaimed space.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


SCHEMA_VERSION = 2
PACKAGED_APP_ID = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"


@dataclass(frozen=True)
class CacheTarget:
    parts: Tuple[str, ...]
    category: str

    @property
    def relative(self) -> str:
        return "\\".join(self.parts)

    @property
    def backup_name(self) -> str:
        return "__".join(self.parts)


CACHE_TARGETS: Tuple[CacheTarget, ...] = (
    CacheTarget(("GrShaderCache",), "gpu"),
    CacheTarget(("ShaderCache",), "gpu"),
    CacheTarget(("GraphiteDawnCache",), "gpu"),
    CacheTarget(("GPUPersistentCache",), "gpu"),
    CacheTarget(("Default", "GPUCache"), "gpu"),
    CacheTarget(("Default", "DawnWebGPUCache"), "gpu"),
    CacheTarget(("Default", "DawnGraphiteCache"), "gpu"),
    CacheTarget(("Default", "Cache"), "http"),
    CacheTarget(("Default", "Code Cache"), "code"),
    CacheTarget(("Default", "Partitions", "codex-browser-app", "GPUCache"), "gpu"),
    CacheTarget(
        ("Default", "Partitions", "codex-browser-app", "DawnWebGPUCache"),
        "gpu",
    ),
    CacheTarget(
        ("Default", "Partitions", "codex-browser-app", "DawnGraphiteCache"),
        "gpu",
    ),
    CacheTarget(("Default", "Partitions", "codex-browser-app", "Cache"), "http"),
    CacheTarget(
        ("Default", "Partitions", "codex-browser-app", "Code Cache"), "code"
    ),
    CacheTarget(("component_crx_cache",), "component"),
    CacheTarget(("extensions_crx_cache",), "component"),
)


PROTECTED_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("Default", "Cookies"),
    ("Default", "Network", "Cookies"),
    ("Default", "Local Storage"),
    ("Default", "IndexedDB"),
    ("Default", "Session Storage"),
    ("Default", "WebStorage"),
    ("Default", "Service Worker"),
    ("Default", "Partitions", "codex-browser-app", "Network"),
    ("Default", "Partitions", "codex-browser-app", "Local Storage"),
    ("Default", "Partitions", "codex-browser-app", "IndexedDB"),
    ("Default", "Partitions", "codex-browser-app", "Session Storage"),
    ("Default", "Partitions", "codex-browser-app", "WebStorage"),
    ("Default", "Partitions", "codex-browser-app", "Service Worker"),
)

# Cache directory names alone are insufficient evidence that their contents are
# disposable. Unexpected state inside an allowlisted directory blocks the run.
PROTECTED_NAMES = frozenset(name.casefold() for name in (
    "auth", "auth.json", "authentication", "credentials", "credentials.json",
    "tokens", "tokens.json", "config", "config.json", "config.toml", ".env",
    "router", "router.json", "router.toml", "memories", "goals", "state",
    "queue", "sessionindex", "session_index.jsonl", "sessions", "skills",
    "plugins", "mcp", "rules", "AGENTS.md", "Cookies", "Login Data",
    "Local State", "Preferences", "Secure Preferences", "Web Data", "History",
    "Network", "Local Storage", "IndexedDB", "Session Storage", "WebStorage",
    "Service Worker",
))
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3", "-wal", "-shm", "-journal", ".wal", ".shm", ".journal")
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MaintenanceError(RuntimeError):
    pass


class AppRunningError(MaintenanceError):
    pass


class PartialCleanupError(MaintenanceError):
    def __init__(self, message: str, report: Dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def default_profile_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Codex" / "web" / "Codex"
    return Path.home() / "AppData" / "Roaming" / "Codex" / "web" / "Codex"


def default_backup_root() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "OpenAI" / "Codex-cache-backups"
    return Path.home() / "AppData" / "Local" / "OpenAI" / "Codex-cache-backups"


def _absolute(path: Path) -> Path:
    # Do not resolve junctions before checking them: that would hide the link.
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _normalized(path: Path) -> str:
    absolute = _absolute(path)
    _check_ancestors(absolute)
    # Resolve only after rejecting reparse points. This also expands Windows
    # short-name aliases before comparing profile/backup containment.
    return os.path.normcase(str(absolute.resolve(strict=False)))


def _lstat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _reject_reparse(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        raise MaintenanceError("Refusing reparse point or symbolic link: {}".format(path))


def _check_ancestors(path: Path) -> None:
    path = _absolute(path)
    for part in (*reversed(path.parents), path):
        info = _lstat(part)
        if info is None:
            continue
        _reject_reparse(part, info)
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise MaintenanceError("Non-directory path ancestor: {}".format(part))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([_normalized(path), _normalized(root)]) == _normalized(
            root
        )
    except ValueError:
        return False


def _safe_profile_path(profile_root: Path, parts: Sequence[str]) -> Path:
    candidate = profile_root.joinpath(*parts)
    if not _is_within(candidate, profile_root):
        raise MaintenanceError("Refusing path outside the Codex profile: {}".format(candidate))
    _check_ancestors(candidate)
    return candidate


def _metadata(info: os.stat_result) -> Tuple[int, ...]:
    # Windows Python 3.13 lstat/fstat disagree on the deprecated st_ctime
    # semantics. Identity, size and modification time are comparable in both.
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_size,
        info.st_mtime_ns, getattr(info, "st_file_attributes", 0),
    )


def _check_protected_file(path: Path, info: os.stat_result) -> None:
    name = path.name.casefold()
    protected_stem = re.fullmatch(
        r"(?:auth|authentication|credentials|tokens?|config|router|memories|goals|state|queue|session[_-]?index|sessions|skills|plugins|mcp|rules)(?:[._-].*)?",
        name,
    )
    if name in PROTECTED_NAMES or protected_stem or name in {"wal", "shm", "journal"} or name.endswith(SQLITE_SUFFIXES):
        raise MaintenanceError("Protected state/database in cache target: {}".format(path))
    if stat.S_ISREG(info.st_mode):
        # SQLite databases can have opaque filenames. Only inspect the signature,
        # never log content. A read failure must block, not silently skip a file.
        with path.open("rb") as stream:
            signature = stream.read(16)
            if _metadata(os.fstat(stream.fileno())) != _metadata(info):
                raise MaintenanceError("Cache changed during inspection: {}".format(path))
        if signature == b"SQLite format 3\x00":
            raise MaintenanceError("SQLite database in cache target: {}".format(path))


def _inspect_tree(path: Path, protect: bool = False) -> Tuple[int, str]:
    _check_ancestors(path)
    total = 0
    digest = hashlib.sha256()
    pending = [path]
    while pending:
        item = pending.pop()
        _check_ancestors(item)
        info = _lstat(item)
        if info is None:
            if item == path:
                return 0, digest.hexdigest()
            raise MaintenanceError("Cache disappeared during inspection: {}".format(item))
        _reject_reparse(item, info)
        if protect:
            _check_protected_file(item, info)
        digest.update(json.dumps([str(item.relative_to(path)), _metadata(info)]).encode("utf-8"))
        if stat.S_ISDIR(info.st_mode):
            _check_ancestors(item)
            with os.scandir(item) as entries:
                pending.extend(sorted((item / entry.name for entry in entries), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            total += int(info.st_size)
        else:
            raise MaintenanceError("Refusing special filesystem object: {}".format(item))
    return total, digest.hexdigest()


def tree_size(path: Path) -> int:
    return _inspect_tree(path)[0]


def _path_report(profile_root: Path, parts: Sequence[str]) -> Dict[str, Any]:
    path = _safe_profile_path(profile_root, parts)
    return {
        "relative_path": "\\".join(parts),
        "path": str(path),
        "exists": path.exists(),
        "bytes": tree_size(path),
    }


def backup_inventory(backup_root: Path) -> Dict[str, Any]:
    _check_ancestors(backup_root)
    entries: List[Dict[str, Any]] = []
    if backup_root.is_dir():
        for item in sorted(backup_root.iterdir(), key=lambda path: path.name, reverse=True):
            _check_ancestors(item)
            if item.is_dir():
                entries.append(
                    {
                        "path": str(item),
                        "bytes": tree_size(item),
                        "manifest": (item / "manifest.json").is_file(),
                    }
                )
    return {
        "path": str(backup_root),
        "count": len(entries),
        "bytes": sum(int(item["bytes"]) for item in entries),
        "entries": entries,
    }


def audit_cache(profile_root: Path, backup_root: Path) -> Dict[str, Any]:
    profile_root = _absolute(profile_root)
    backup_root = _absolute(backup_root)
    _check_ancestors(profile_root)
    _check_ancestors(backup_root)
    targets: List[Dict[str, Any]] = []
    for target in CACHE_TARGETS:
        item = _path_report(profile_root, target.parts)
        item["category"] = target.category
        targets.append(item)
    protected = [_path_report(profile_root, parts) for parts in PROTECTED_PATHS]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "profile_root": str(profile_root),
        "profile_present": profile_root.is_dir(),
        "targets": targets,
        "target_bytes": sum(int(item["bytes"]) for item in targets),
        "protected_paths": protected,
        "backup_inventory": backup_inventory(backup_root),
    }


def chatgpt_running() -> bool:
    """Legacy public name; checks Codex desktop, CLI and helper executables.

    An unparseable/incomplete process query is an error, never evidence that the
    application has stopped. The helper does not terminate processes.
    """
    if os.name != "nt":
        raise MaintenanceError("Process verification requires Windows.")
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="oem",
            errors="strict",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise MaintenanceError("Cannot verify running processes: {}".format(type(error).__name__)) from error
    if result.returncode != 0 or result.stderr.strip() or not result.stdout.strip():
        raise MaintenanceError("Cannot verify running processes: tasklist failed or returned no data.")
    try:
        rows = list(csv.reader(io.StringIO(result.stdout.lstrip("\ufeff")), strict=True))
    except csv.Error as error:
        raise MaintenanceError("Cannot verify running processes: invalid tasklist CSV.") from error
    names = []
    for row in rows:
        if not row:
            continue
        if len(row) != 5 or not row[0].strip() or not row[1].isdigit():
            raise MaintenanceError("Cannot verify running processes: unexpected tasklist row.")
        names.append(row[0].casefold())
    if not names:
        raise MaintenanceError("Cannot verify running processes: no process records.")
    return any(re.fullmatch(r"(?:openai\.)?(?:codex|chatgpt)(?:[-_ .].*)?\.exe", name) for name in names)


def wait_until_stopped(timeout_seconds: int, running_check: Callable[[], bool]) -> None:
    if timeout_seconds <= 0:
        if running_check():
            raise AppRunningError(
                "Codex/ChatGPT or a Codex CLI/helper is running. Fully exit desktop and CLI sessions before cleanup."
            )
        return
    deadline = time.monotonic() + timeout_seconds
    while running_check():
        if time.monotonic() >= deadline:
            raise AppRunningError(
                "Timed out waiting for Codex/ChatGPT to exit after {} seconds.".format(
                    timeout_seconds
                )
            )
        time.sleep(2)
    time.sleep(3)
    if running_check():
        raise AppRunningError("Codex/ChatGPT restarted before cleanup could begin.")


def _unique_backup_dir(backup_root: Path, now: Optional[dt.datetime]) -> Path:
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = backup_root / stamp
    suffix = 1
    while _lstat(candidate) is not None:
        candidate = backup_root / "{}-{}".format(stamp, suffix)
        suffix += 1
    return candidate


def _write_manifest(backup_dir: Path, report: Dict[str, Any]) -> None:
    _check_ancestors(backup_dir)
    manifest = backup_dir / "manifest.json"
    _check_ancestors(manifest)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=backup_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _check_ancestors(manifest)
        _check_ancestors(temporary)
        os.replace(temporary, manifest)
    finally:
        if _lstat(temporary) is not None:
            _check_ancestors(temporary)
            temporary.unlink()


def _existing_ancestor(path: Path) -> Path:
    _check_ancestors(path)
    for candidate in (path, *path.parents):
        if _lstat(candidate) is not None:
            return candidate
    raise MaintenanceError("No existing volume for {}".format(path))


def _space_before(profile_root: Path, backup_root: Path) -> Dict[str, Any]:
    destination_volume_path = _existing_ancestor(backup_root)
    same_volume = profile_root.stat().st_dev == destination_volume_path.stat().st_dev
    return {
        "same_volume_backup": same_volume,
        "profile_free_before": shutil.disk_usage(profile_root).free,
        "backup_free_before": shutil.disk_usage(destination_volume_path).free,
        "note": "Moving to a backup on the same volume does not reclaim space. Observed free-space changes can include other activity; moved_bytes is logical file size, not freed space.",
    }


def _record_failure(report: Dict[str, Any], relative_path: str, stage: str, error: Exception) -> None:
    report["status"] = "partial"
    report["failures"].append({"relative_path": relative_path, "stage": stage, "error": str(error)})


def _complete_observations(profile_root: Path, backup_root: Path, report: Dict[str, Any]) -> None:
    report["moved_bytes"] = sum(int(item["bytes"]) for item in report["moved"])
    try:
        report["protected_after"] = [_path_report(profile_root, parts) for parts in PROTECTED_PATHS]
        if report["protected_after"] != report["protected_before"]:
            raise MaintenanceError("Protected-path sizes or presence changed during cleanup.")
    except (MaintenanceError, OSError) as error:
        _record_failure(report, "<protected paths>", "verification", error)
    try:
        _check_ancestors(profile_root)
        _check_ancestors(backup_root)
        space = report["space"]
        space["profile_free_after"] = shutil.disk_usage(profile_root).free
        space["backup_free_after"] = shutil.disk_usage(_existing_ancestor(backup_root)).free
        space["observed_profile_free_delta"] = space["profile_free_after"] - space["profile_free_before"]
        space["observed_backup_free_delta"] = space["backup_free_after"] - space["backup_free_before"]
        space["observed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    except (MaintenanceError, OSError) as error:
        _record_failure(report, "<volume space>", "verification", error)
    report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def clean_cache(
    profile_root: Path,
    backup_root: Path,
    wait_seconds: int = 0,
    running_check: Callable[[], bool] = chatgpt_running,
    require_stopped: bool = True,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Move approved cache trees to a same-volume backup; never delete backups.

    require_stopped=False and running_check are internal fixture-test seams.
    There is deliberately no corresponding command-line bypass.
    """
    profile_root = _absolute(profile_root)
    backup_root = _absolute(backup_root)
    _check_ancestors(profile_root)
    _check_ancestors(backup_root)
    if not profile_root.is_dir():
        raise MaintenanceError("Codex profile not found: {}".format(profile_root))
    if _is_within(backup_root, profile_root) or _is_within(profile_root, backup_root):
        raise MaintenanceError("Backup root must be separate from the Codex profile.")
    if require_stopped:
        wait_until_stopped(wait_seconds, running_check)

    candidates: List[Tuple[CacheTarget, Path, int, str]] = []
    skipped: List[str] = []
    for target in CACHE_TARGETS:
        source = _safe_profile_path(profile_root, target.parts)
        if _lstat(source) is not None:
            byte_count, fingerprint = _inspect_tree(source, protect=True)
            candidates.append((target, source, byte_count, fingerprint))
        else:
            skipped.append(target.relative)

    protected_before = [_path_report(profile_root, parts) for parts in PROTECTED_PATHS]
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "no-op" if not candidates else "running",
        "profile_root": str(profile_root),
        "backup_dir": None,
        "moved": [],
        "moved_bytes": 0,
        "operations": [],
        "skipped": skipped,
        "failures": [],
        "protected_before": protected_before,
        "protected_after": [],
        "space": _space_before(profile_root, backup_root),
    }
    if not candidates:
        _complete_observations(profile_root, backup_root, report)
        if report["failures"]:
            raise PartialCleanupError("Cache inspection failed; nothing was moved.", report)
        return report

    # A cross-volume shutil.move falls back to recursive copy/delete. This
    # legacy helper deliberately uses atomic renames instead of that fallback.
    if not report["space"]["same_volume_backup"]:
        raise MaintenanceError("Backup must be on the same volume; cross-volume copy/delete is not supported.")
    if require_stopped:
        wait_until_stopped(0, running_check)
    _check_ancestors(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    _check_ancestors(backup_root)
    backup_dir = _unique_backup_dir(backup_root, now)
    backup_dir.mkdir(parents=False)
    _check_ancestors(backup_dir)
    report["backup_dir"] = str(backup_dir)
    for target, source, byte_count, fingerprint in candidates:
        report["operations"].append({
            "relative_path": target.relative, "category": target.category,
            "source_path": str(source), "backup_path": str(backup_dir / target.backup_name),
            "bytes": byte_count, "fingerprint": fingerprint, "status": "pending",
        })
    try:
        _write_manifest(backup_dir, report)
    except (MaintenanceError, OSError) as error:
        _record_failure(report, "manifest.json", "initial journal", error)
        _complete_observations(profile_root, backup_root, report)
        raise PartialCleanupError("Cannot write the initial manifest; nothing was moved.", report) from error

    for operation, (target, source, byte_count, fingerprint) in zip(report["operations"], candidates):
        destination = backup_dir / target.backup_name
        stage = "preflight"
        try:
            _check_ancestors(source)
            _check_ancestors(destination)
            if _lstat(destination) is not None:
                raise MaintenanceError("Backup destination already exists: {}".format(destination))
            if _inspect_tree(source, protect=True) != (byte_count, fingerprint):
                raise MaintenanceError("Cache changed since preflight: {}".format(source))
            if require_stopped:
                wait_until_stopped(0, running_check)
            operation["status"] = "prepared"
            stage = "write-ahead journal"
            _write_manifest(backup_dir, report)
            # Validate once more after the journal write. A crash after rename
            # leaves a prepared record with both paths for manual recovery.
            stage = "final preflight"
            if require_stopped:
                wait_until_stopped(0, running_check)
            _check_ancestors(destination)
            if _lstat(destination) is not None:
                raise MaintenanceError("Backup destination appeared before move: {}".format(destination))
            if _inspect_tree(source, protect=True) != (byte_count, fingerprint):
                raise MaintenanceError("Cache changed immediately before move: {}".format(source))
            stage = "rename"
            os.rename(source, destination)
            operation["status"] = "moved"
            report["moved"].append(
                {
                    "relative_path": target.relative,
                    "category": target.category,
                    "bytes": byte_count,
                    "backup_path": str(destination),
                }
            )
            report["moved_bytes"] += byte_count
            stage = "post-move journal"
            _write_manifest(backup_dir, report)
        except (MaintenanceError, OSError) as error:
            if operation["status"] != "moved":
                operation["status"] = "failed"
            _record_failure(report, target.relative, stage, error)
            try:
                operation["source_present"] = _lstat(source) is not None
                operation["backup_present"] = _lstat(destination) is not None
            except OSError as observation_error:
                _record_failure(report, target.relative, "failure observation", observation_error)
            _complete_observations(profile_root, backup_root, report)
            try:
                _write_manifest(backup_dir, report)
            except (MaintenanceError, OSError) as journal_error:
                _record_failure(report, "manifest.json", "failure journal", journal_error)
                report["manifest_warning"] = "The last atomic manifest may still say prepared/running. Inspect source and backup paths before recovery; do not rerun blindly."
            raise PartialCleanupError(
                "Cache cleanup stopped; inspect the manifest and report before recovery: {}".format(error), report
            ) from error

    report["status"] = "completed"
    _complete_observations(profile_root, backup_root, report)
    try:
        _write_manifest(backup_dir, report)
    except (MaintenanceError, OSError) as error:
        _record_failure(report, "manifest.json", "completion journal", error)
        report["manifest_warning"] = "The last atomic manifest is retained, but completion could not be recorded."
    if report["failures"]:
        raise PartialCleanupError("Cache moves finished, but verification or journaling failed.", report)
    return report


def relaunch_packaged_app() -> Optional[str]:
    if os.name != "nt":
        return "Relaunch skipped because the current platform is not Windows."
    try:
        subprocess.Popen(
            ["explorer.exe", PACKAGED_APP_ID],
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return "Cache cleanup succeeded, but Codex relaunch failed: {}".format(error)
    return None


def mib(byte_count: int) -> str:
    return "{:.2f} MiB".format(byte_count / (1024 * 1024))


def print_audit(report: Dict[str, Any]) -> None:
    print("Codex cache audit (read-only)")
    print("Profile: {}".format(report["profile_root"]))
    print("Allowlisted cache candidates: {} (cleanup requires further safety checks)".format(mib(int(report["target_bytes"]))))
    print("Existing backups: {} ({})".format(
        report["backup_inventory"]["count"],
        mib(int(report["backup_inventory"]["bytes"])),
    ))
    for item in report["targets"]:
        if item["exists"]:
            print("- {:<16} {:>10}  {}".format(
                item["category"], mib(int(item["bytes"])), item["relative_path"]
            ))


def print_cleanup(report: Dict[str, Any]) -> None:
    print("Codex cache cleanup: {}".format(report["status"]))
    print("Moved: {} item(s), {}".format(
        len(report["moved"]), mib(int(report.get("moved_bytes", 0)))
    ))
    if report.get("backup_dir"):
        print("Backup: {}".format(report["backup_dir"]))
    space = report.get("space", {})
    if space.get("same_volume_backup"):
        print("Same-volume backup: moved bytes do not represent reclaimed disk space.")
    if "observed_profile_free_delta" in space:
        print("Observed profile-volume free space: {} before, {} after; change {} (may include other activity).".format(
            mib(space["profile_free_before"]), mib(space["profile_free_after"]),
            mib(space["observed_profile_free_delta"]),
        ))
    if report.get("manifest_warning"):
        print("MANIFEST WARNING: {}".format(report["manifest_warning"]))
    if report.get("failures"):
        for failure in report["failures"]:
            print("FAILED {}: {}".format(failure["relative_path"], failure["error"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or safely back up and clear rebuildable Codex desktop caches."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "clean"):
        command = subparsers.add_parser(name)
        command.add_argument("--profile-root", type=Path, default=default_profile_root())
        command.add_argument("--backup-root", type=Path, default=default_backup_root())
        command.add_argument("--json", action="store_true")
    clean = subparsers.choices["clean"]
    clean.add_argument(
        "--apply",
        action="store_true",
        help="Required confirmation flag for moving cache directories into a backup.",
    )
    clean.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Wait this long for Codex/ChatGPT desktop, CLI and helpers to exit before cleanup.",
    )
    clean.add_argument("--relaunch", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            report = audit_cache(args.profile_root, args.backup_root)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print_audit(report)
            return 0
        if not args.apply:
            raise MaintenanceError("Refusing cleanup without the explicit --apply flag.")
        report = clean_cache(
            args.profile_root,
            args.backup_root,
            wait_seconds=max(0, args.wait_seconds),
        )
        warning = relaunch_packaged_app() if args.relaunch else None
        if warning:
            report["relaunch_warning"] = warning
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_cleanup(report)
            if warning:
                print(warning)
        return 0
    except PartialCleanupError as error:
        if getattr(args, "json", False):
            print(json.dumps(error.report, ensure_ascii=False, indent=2))
        else:
            print_cleanup(error.report)
        return 4
    except (MaintenanceError, OSError) as error:
        print("Codex cache maintenance failed: {}".format(error), file=sys.stderr)
        return 3 if isinstance(error, AppRunningError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
