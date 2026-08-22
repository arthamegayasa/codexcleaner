#!/usr/bin/env python3
"""Audit and safely move rebuildable Codex desktop caches on Windows.

The cleanup is allowlist-based. It never touches task history, cookies,
authentication storage, Local Storage, IndexedDB, or project files. Cache
directories are moved into a timestamped backup instead of being deleted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
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
    CacheTarget(
        ("Default", "Partitions", "codex-browser-app", "Service Worker"),
        "service-worker",
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
    ("Default", "Partitions", "codex-browser-app", "Network"),
    ("Default", "Partitions", "codex-browser-app", "Local Storage"),
    ("Default", "Partitions", "codex-browser-app", "IndexedDB"),
    ("Default", "Partitions", "codex-browser-app", "Session Storage"),
    ("Default", "Partitions", "codex-browser-app", "WebStorage"),
)


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


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


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
    if candidate.exists() and candidate.resolve() != candidate.absolute() and not _is_within(
        candidate.resolve(), profile_root
    ):
        raise MaintenanceError("Refusing cache path that resolves outside profile: {}".format(candidate))
    return candidate


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink():
        return int(path.lstat().st_size)
    if path.is_file():
        return int(path.stat().st_size)
    total = 0
    for root, directories, files in os.walk(str(path), followlinks=False):
        root_path = Path(root)
        directories[:] = [name for name in directories if not (root_path / name).is_symlink()]
        for name in files:
            item = root_path / name
            try:
                total += int(item.lstat().st_size)
            except OSError:
                continue
    return total


def _path_report(profile_root: Path, parts: Sequence[str]) -> Dict[str, Any]:
    path = _safe_profile_path(profile_root, parts)
    return {
        "relative_path": "\\".join(parts),
        "path": str(path),
        "exists": path.exists(),
        "bytes": tree_size(path),
    }


def backup_inventory(backup_root: Path) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    if backup_root.is_dir():
        for item in sorted(backup_root.iterdir(), key=lambda path: path.name, reverse=True):
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
    profile_root = profile_root.expanduser().resolve(strict=False)
    backup_root = backup_root.expanduser().resolve(strict=False)
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
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq ChatGPT.exe", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return True
    return "ChatGPT.exe".casefold() in result.stdout.casefold()


def wait_until_stopped(timeout_seconds: int, running_check: Callable[[], bool]) -> None:
    if timeout_seconds <= 0:
        if running_check():
            raise AppRunningError(
                "Codex/ChatGPT is running. Fully exit it from the system tray before cleanup."
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
    while candidate.exists():
        candidate = backup_root / "{}-{}".format(stamp, suffix)
        suffix += 1
    return candidate


def _write_manifest(backup_dir: Path, report: Dict[str, Any]) -> None:
    (backup_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def clean_cache(
    profile_root: Path,
    backup_root: Path,
    wait_seconds: int = 0,
    running_check: Callable[[], bool] = chatgpt_running,
    require_stopped: bool = True,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    profile_root = profile_root.expanduser().resolve(strict=False)
    backup_root = backup_root.expanduser().resolve(strict=False)
    if not profile_root.is_dir():
        raise MaintenanceError("Codex profile not found: {}".format(profile_root))
    if _is_within(backup_root, profile_root) or _is_within(profile_root, backup_root):
        raise MaintenanceError("Backup root must be separate from the Codex profile.")
    if require_stopped:
        wait_until_stopped(wait_seconds, running_check)

    candidates: List[Tuple[CacheTarget, Path, int]] = []
    skipped: List[str] = []
    for target in CACHE_TARGETS:
        source = _safe_profile_path(profile_root, target.parts)
        if source.is_symlink():
            raise MaintenanceError("Refusing symbolic-link cache target: {}".format(source))
        if source.exists():
            candidates.append((target, source, tree_size(source)))
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
        "skipped": skipped,
        "failures": [],
        "protected_before": protected_before,
        "protected_after": [],
    }
    if not candidates:
        report["protected_after"] = protected_before
        report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return report

    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = _unique_backup_dir(backup_root, now)
    backup_dir.mkdir(parents=False)
    report["backup_dir"] = str(backup_dir)
    _write_manifest(backup_dir, report)

    for target, source, byte_count in candidates:
        destination = backup_dir / target.backup_name
        try:
            shutil.move(str(source), str(destination))
            report["moved"].append(
                {
                    "relative_path": target.relative,
                    "category": target.category,
                    "bytes": byte_count,
                    "backup_path": str(destination),
                }
            )
            _write_manifest(backup_dir, report)
        except (OSError, shutil.Error) as error:
            report["status"] = "partial"
            report["failures"].append(
                {"relative_path": target.relative, "error": str(error)}
            )
            report["protected_after"] = [
                _path_report(profile_root, parts) for parts in PROTECTED_PATHS
            ]
            report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            _write_manifest(backup_dir, report)
            raise PartialCleanupError(
                "Cache cleanup stopped after a partial move: {}".format(error), report
            )

    report["status"] = "completed"
    report["moved_bytes"] = sum(int(item["bytes"]) for item in report["moved"])
    report["protected_after"] = [
        _path_report(profile_root, parts) for parts in PROTECTED_PATHS
    ]
    report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_manifest(backup_dir, report)
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
    print("Rebuildable cache: {}".format(mib(int(report["target_bytes"]))))
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
        help="Wait this long for ChatGPT.exe to exit before starting cleanup.",
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
