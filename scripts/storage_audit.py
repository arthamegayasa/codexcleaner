#!/usr/bin/env python3
"""Bounded, metadata-only storage inventory. Nothing in this module deletes data.

Sizes are logical file lengths, including sparse-file lengths and repeated hardlinks.
Classification is an aid for review, never evidence that an item can be deleted.
Only explicitly requested JSON/Markdown reports are written by the CLI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import heapq
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Optional, Sequence

REPARSE_POINT = 0x400
CATEGORIES = ("cache_candidate", "review_artifact", "protected_state", "project", "unknown")
PROJECT_MARKERS = (".git", ".hg", ".svn", "pyproject.toml", "package.json",
                   "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "AGENTS.md")
PROJECT_MARKER_NAMES = frozenset(name.casefold() for name in PROJECT_MARKERS)


def _absolute(path: Path) -> Path:
    # resolve() would follow links before we have checked their ancestors.
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


def _inside(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((_key(path), _key(parent))) == _key(parent)
    except ValueError:
        return False


def _is_reparse(info: Any) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _checked_stat(path: Path) -> tuple[Optional[Any], Optional[Path]]:
    """Check ancestors from the volume root down, without first following links."""
    info = None
    for part in (*reversed(path.parents), path):
        info = os.lstat(part)
        if _is_reparse(info):
            return None, part
    return info, None


def _identity(info: Any) -> Optional[tuple[int, int]]:
    # Windows DirEntry.stat can report zero IDs; never deduplicate those values.
    device, inode = getattr(info, "st_dev", 0), getattr(info, "st_ino", 0)
    return (device, inode) if inode else None


def _timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def classify_path(path: Path, *, project_boundary: Optional[Path] = None) -> tuple[str, str]:
    """Conservative, name-based labels; no label authorizes deletion."""
    if project_boundary is not None and _inside(path, project_boundary):
        category, reason = classify_path(path)
        if category == "protected_state":
            return category, reason
        return "project", f"Within discovered project boundary: {project_boundary}; preserve work and future reuse."
    parts = [p.casefold() for p in str(path).replace("\\", "/").split("/") if p]
    name = parts[-1] if parts else ""
    protected = {"sessions", "archived_sessions", "sqlite", "auth", "credentials",
                 "secrets", ".sandbox-secrets", "codex-router", ".opencodex",
                 "responses-state-spill", "automations", "rules", "skills", ".git", ".hg", ".svn"}
    if (set(parts) & protected or name in {"auth.json", "config.toml", "config.json", ".env"}
            or name.startswith(".env.")
            or re.search(r"\.(?:sqlite\d*|db)(?:-wal|-shm|-journal)?$", name)
            or name.endswith((".vhd", ".vhdx", ".key", ".pem", ".keystore"))):
        return "protected_state", "Application state, history, credentials, database, or repository metadata."
    if (set(parts) & {"worktrees", "projects", "playground"}
            or any(parts[i:i + 2] == ["documents", "codex"] for i in range(len(parts) - 1))):
        return "project", "Project/worktree location; local work and future reuse must be preserved."
    if set(parts) & {"generated_images", "visualizations", "attachments", "outputs", "artifacts"}:
        return "review_artifact", "Potentially unique user output or attachment; not disposable cache."
    if any(re.match(r"neuroguide[ _.-]+(?:reports?|research)(?:[ _.-]|$)", part) for part in parts):
        return "review_artifact", "NeuroGuide report/research bundle; retain unique results and review its role before selecting anything."
    if any(p.startswith("codex-runtime-install-") for p in parts):
        return "cache_candidate", "Runtime installation staging; activity and ownership need verification."
    if set(parts) & {"codex-runtimes", "packages", "plugins", ".sandbox-bin", ".sandbox"}:
        return "protected_state", "Installed runtime or application component; not a general cleanup target."
    if set(parts) & {"cache", ".cache", "temp", "tmp", ".tmp"}:
        return "cache_candidate", "Cache/temporary location only; name and age do not establish deletability."
    if name == ".codex" or ("codex" in parts and ("roaming" in parts or "openai" in parts)):
        return "protected_state", "Application data root; contains mixed state and user data."
    return "unknown", "Purpose cannot be established from metadata alone."


def _ancestor_project_boundary(path: Path) -> Optional[Path]:
    """Find project markers for a requested root, including a nested root.

    Root ancestors have already passed the reparse check. Only exact marker
    metadata is inspected; Git worktree pointer files are never opened.
    """
    for parent in (path, *path.parents):
        for name in PROJECT_MARKERS:
            try:
                info = os.lstat(parent / name)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                return parent
    return None


def stable_temp_prefix(name: str) -> Optional[str]:
    """Group NeuroGuide temp siblings, stripping only recognizable random/date tails."""
    if not name.casefold().startswith("neuroguide"):
        return None
    value = name
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    value = re.sub(r"[ _.-]+" + uuid + r"$", "", value, flags=re.I)
    while True:
        match = re.search(r"[ _-]+([A-Za-z0-9]{6,32})$", value)
        if not match:
            break
        tail = match.group(1)
        random_tail = (bool(re.fullmatch(r"[0-9a-f]{8,32}", tail, re.I))
                       or tail.isdigit()
                       or (any(c.isdigit() for c in tail) and any(c.isalpha() for c in tail)
                           and not re.fullmatch(r"v\d+", tail, re.I)))
        if not random_tail:
            break
        value = value[:match.start()]
    return value


def discover_roots() -> list[Path]:
    profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    local = Path(os.environ.get("LOCALAPPDATA", str(profile / "AppData" / "Local")))
    roaming = Path(os.environ.get("APPDATA", str(profile / "AppData" / "Roaming")))
    roots = [profile / ".codex", local / "Temp", profile / ".cache" / "codex-runtimes",
             profile / "Documents" / "Codex", roaming / "Codex", local / "OpenAI" / "Codex"]
    if os.environ.get("CODEX_HOME"):
        roots.append(Path(os.environ["CODEX_HOME"]))
    return roots


def audit(roots: list[Path], *, max_entries: int = 500_000, max_directories: int = 50_000,
          max_depth: int = 64, max_seconds: float = 120.0,
          top_limit: int = 40, largest_limit: int = 40, detail_limit: int = 200) -> dict[str, Any]:
    """Inventory exact directory roots without opening file contents.

    The bounds are cooperative: they cannot interrupt a blocked filesystem syscall.
    Missing roots, unreadable directories, skipped links, and limits are explicit.
    Parent roots cover nested requests regardless of their order in ``roots``.
    """
    if (any(v <= 0 for v in (max_entries, max_directories, max_seconds, top_limit, largest_limit, detail_limit))
            or max_depth < 0 or not math.isfinite(max_seconds)):
        raise ValueError("Audit limits must be positive (max_depth may be zero).")
    started = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": 1, "generated_at": _timestamp(time.time()), "read_only": True,
        "measurement": {"kind": "logical_file_size", "unit": "bytes", "hardlinks_deduplicated": False,
                        "physical_bytes": None, "recoverable_bytes": None,
                        "note": "Logical lengths, including sparse files. Not physical usage or recoverable space."},
        "classification_notice": "Every label is for review only. Name, age, and category never authorize deletion.",
        "limits": {"max_entries": max_entries, "max_directories": max_directories,
                   "max_depth": max_depth, "max_seconds": max_seconds, "top_limit": top_limit,
                   "largest_limit": largest_limit, "detail_limit": detail_limit},
        "roots": [], "errors": [], "skipped_paths": [], "limits_reached": [],
    }
    counts = {"error_count": 0, "skipped_path_count": 0, "entries_examined": 0}
    records: list[dict[str, Any]] = []
    selected: list[tuple[Path, dict[str, Any]]] = []
    largest: list[tuple[int, str, float, str, str]] = []
    seen_directories: dict[tuple[int, int], str] = {}
    root_states: list[dict[str, Any]] = []
    root_project_boundaries: dict[Path, Optional[Path]] = {}

    def error(path: Path, exc: OSError) -> None:
        counts["error_count"] += 1
        if len(report["errors"]) < detail_limit:
            report["errors"].append({"path": str(path), "type": type(exc).__name__, "message": str(exc)})

    def skipped(path: Path, reason: str, **extra: Any) -> None:
        counts["skipped_path_count"] += 1
        if len(report["skipped_paths"]) < detail_limit:
            report["skipped_paths"].append({"path": str(path), "reason": reason, **extra})

    def limit(name: str) -> None:
        if name not in report["limits_reached"]:
            report["limits_reached"].append(name)

    def exhausted() -> bool:
        if counts["entries_examined"] >= max_entries:
            limit("max_entries")
        if time.monotonic() - started >= max_seconds:
            limit("max_seconds")
        return bool(set(report["limits_reached"]) & {"max_entries", "max_seconds", "max_directories"})

    # Validate root ancestors before considering overlaps, so an alias is never silently resolved.
    for raw in roots:
        path = _absolute(Path(raw))
        category, reason = classify_path(path)
        state = {"path": str(path), "status": "pending", "category": category,
                 "classification_reason": reason, "deletion_allowed": False,
                 "logical_bytes": 0, "own_logical_bytes": 0, "file_count": 0, "directory_count": 0,
                 "newest_descendant_file_modified": None,
                 "category_totals": {c: {"logical_bytes": 0, "file_count": 0} for c in CATEGORIES},
                 "immediate_directories": [], "immediate_directories_omitted": 0,
                 "top_directories": [], "top_directories_omitted": 0}
        report["roots"].append(state)
        if exhausted():
            state["status"] = "not_scanned_limit"
            continue
        try:
            info, blocked = _checked_stat(path)
            if blocked:
                state["status"] = "skipped_reparse"
                skipped(path, "reparse_or_symlink_ancestor", blocked_path=str(blocked))
            elif info is None or not stat.S_ISDIR(info.st_mode):
                state["status"] = "not_directory"
                skipped(path, "root_is_not_directory")
            else:
                boundary = _ancestor_project_boundary(path)
                root_project_boundaries[path] = boundary
                state["category"], state["classification_reason"] = classify_path(path, project_boundary=boundary)
                selected.append((path, state))
        except OSError as exc:
            state["status"] = "missing" if isinstance(exc, FileNotFoundError) else "error"
            error(path, exc)

    effective: list[tuple[Path, dict[str, Any]]] = []
    for path, state in sorted(selected, key=lambda item: len(item[0].parts)):
        covering = next((p for p, _ in effective if _inside(path, p)), None)
        if covering is not None:
            state.update(status="covered_by_root", covered_by=str(covering))
            skipped(path, "overlapping_root", covered_by=str(covering))
        else:
            effective.append((path, state))

    def mark_project(row: dict[str, Any], boundary: Path) -> None:
        """A late marker must also update files seen earlier in this directory."""
        row["_project_boundary"] = boundary
        row_path = Path(row["path"])
        row["category"], row["classification_reason"] = classify_path(row_path, project_boundary=boundary)
        for category in CATEGORIES:
            if category not in {"project", "protected_state"}:
                for field in ("logical_bytes", "file_count"):
                    row["category_totals"]["project"][field] += row["category_totals"][category][field]
                    row["category_totals"][category][field] = 0
        for index, (size, file_path, modified, category, reason) in enumerate(largest):
            if Path(file_path).parent == row_path:
                category, reason = classify_path(Path(file_path), project_boundary=boundary)
                largest[index] = (size, file_path, modified, category, reason)
        heapq.heapify(largest)

    for root_path, state in effective:
        if exhausted():
            state["status"] = "not_scanned_limit"
            continue
        state["status"] = "scanned"
        before_errors = counts["error_count"]
        root_number = len(root_states)
        root_states.append(state)
        stack: list[tuple[Path, Optional[int], int]] = [(root_path, None, 0)]
        while stack:
            if exhausted():
                state["status"] = "partial"
                break
            path, parent_index, depth = stack.pop()
            if len(records) >= max_directories:
                limit("max_directories")
                state["status"] = "partial"
                break
            try:
                info, blocked = _checked_stat(path)
                if blocked:
                    skipped(path, "reparse_or_symlink_ancestor", blocked_path=str(blocked))
                    if parent_index is None:
                        state["status"] = "skipped_reparse"
                    continue
                if info is None or not stat.S_ISDIR(info.st_mode):
                    skipped(path, "directory_changed_type")
                    state["status"] = "partial"
                    continue
                identity = _identity(info)
                if identity is not None and identity in seen_directories:
                    skipped(path, "directory_alias", covered_by=seen_directories[identity])
                    if parent_index is None:
                        state.update(status="covered_by_alias", covered_by=seen_directories[identity])
                    continue
                if identity is not None:
                    seen_directories[identity] = str(path)
                boundary = (records[parent_index]["_project_boundary"] if parent_index is not None
                            else root_project_boundaries[root_path])
                category, reason = classify_path(path, project_boundary=boundary)
                row = {"path": str(path), "depth": depth, "logical_bytes": 0, "own_logical_bytes": 0,
                       "file_count": 0, "directory_count": 0, "category": category,
                       "classification_reason": reason, "deletion_allowed": False,
                       "category_totals": {c: {"logical_bytes": 0, "file_count": 0} for c in CATEGORIES},
                       "_newest": None, "_parent": parent_index, "_root": root_number,
                       "_project_boundary": boundary}
                index = len(records)
                records.append(row)
                with os.scandir(path) as entries:
                    for entry in entries:
                        if exhausted():
                            state["status"] = "partial"
                            break
                        counts["entries_examined"] += 1
                        entry_path = Path(entry.path)
                        try:
                            entry_info = entry.stat(follow_symlinks=False)
                            if (row["_project_boundary"] is None
                                    and entry_path.name.casefold() in PROJECT_MARKER_NAMES
                                    and (stat.S_ISDIR(entry_info.st_mode) or stat.S_ISREG(entry_info.st_mode)
                                         or _is_reparse(entry_info))):
                                mark_project(row, path)
                            if _is_reparse(entry_info):
                                skipped(entry_path, "reparse_or_symlink")
                            elif stat.S_ISDIR(entry_info.st_mode):
                                if depth >= max_depth:
                                    limit("max_depth")
                                    skipped(entry_path, "depth_limit")
                                    state["status"] = "partial"
                                else:
                                    stack.append((entry_path, index, depth + 1))
                            elif stat.S_ISREG(entry_info.st_mode):
                                size = int(entry_info.st_size)
                                modified = float(entry_info.st_mtime)
                                row["logical_bytes"] += size
                                row["own_logical_bytes"] += size
                                row["file_count"] += 1
                                row["_newest"] = modified if row["_newest"] is None else max(row["_newest"], modified)
                                file_category, file_reason = classify_path(entry_path, project_boundary=row["_project_boundary"])
                                row["category_totals"][file_category]["logical_bytes"] += size
                                row["category_totals"][file_category]["file_count"] += 1
                                item = (size, str(entry_path), modified, file_category, file_reason)
                                if len(largest) < largest_limit:
                                    heapq.heappush(largest, item)
                                elif item > largest[0]:
                                    heapq.heapreplace(largest, item)
                            else:
                                skipped(entry_path, "not_regular_file_or_directory")
                        except OSError as exc:
                            error(entry_path, exc)
            except OSError as exc:
                error(path, exc)
                if parent_index is None:
                    state["status"] = "error"
        if counts["error_count"] > before_errors and state["status"] == "scanned":
            state["status"] = "partial"

    for row in reversed(records):
        if row["_parent"] is not None:
            parent = records[row["_parent"]]
            parent["logical_bytes"] += row["logical_bytes"]
            parent["file_count"] += row["file_count"]
            parent["directory_count"] += row["directory_count"] + 1
            for category in CATEGORIES:
                for field in ("logical_bytes", "file_count"):
                    parent["category_totals"][category][field] += row["category_totals"][category][field]
            if row["_newest"] is not None:
                parent["_newest"] = row["_newest"] if parent["_newest"] is None else max(parent["_newest"], row["_newest"])

    def public(row: dict[str, Any]) -> dict[str, Any]:
        return {**{k: v for k, v in row.items() if not k.startswith("_")},
                "newest_descendant_file_modified": _timestamp(row["_newest"])}

    for number, state in enumerate(root_states):
        own_rows = [r for r in records if r["_root"] == number]
        if not own_rows:
            continue
        state.update({k: v for k, v in public(own_rows[0]).items() if k not in {"depth"}})
        children = sorted((r for r in own_rows if r["depth"] == 1), key=lambda r: r["logical_bytes"], reverse=True)
        descendants = sorted((r for r in own_rows if r["depth"] > 0), key=lambda r: r["logical_bytes"], reverse=True)
        state["immediate_directories"] = [public(r) for r in children[:top_limit]]
        state["immediate_directories_omitted"] = max(0, len(children) - top_limit)
        state["top_directories"] = [public(r) for r in descendants[:top_limit]]
        state["top_directories_omitted"] = max(0, len(descendants) - top_limit)

    groups: dict[str, dict[str, Any]] = {}
    for row in records:
        path = Path(row["path"])
        if path.parent.name.casefold() not in {"temp", "tmp", ".tmp"}:
            continue
        prefix = stable_temp_prefix(path.name)
        if prefix is None:
            continue
        group_key = _key(path.parent) + "|" + prefix.casefold()
        group = groups.setdefault(group_key, {"parent": str(path.parent), "prefix": prefix,
                    "logical_bytes": 0, "file_count": 0, "directory_count": 0,
                    "member_count": 0, "members": [], "members_omitted": 0,
                    "category": row["category"], "deletion_allowed": False,
                    "notice": "Grouping only: random-looking suffixes do not prove disposable contents."})
        category_priority = {"unknown": 0, "cache_candidate": 1, "review_artifact": 2, "project": 3, "protected_state": 4}
        if category_priority[row["category"]] > category_priority[group["category"]]:
            group["category"] = row["category"]
        for field in ("logical_bytes", "file_count"):
            group[field] += row[field]
        group["directory_count"] += row["directory_count"] + 1
        group["member_count"] += 1
        if len(group["members"]) < detail_limit:
            group["members"].append(str(path))
        else:
            group["members_omitted"] += 1
    report["temp_groups"] = sorted(groups.values(), key=lambda g: g["logical_bytes"], reverse=True)[:top_limit]
    report["temp_groups_omitted"] = max(0, len(groups) - top_limit)
    report["largest_files"] = [{"path": path, "logical_bytes": size, "modified": _timestamp(modified),
                                "category": category, "classification_reason": reason, "deletion_allowed": False}
                               for size, path, modified, category, reason in sorted(largest, reverse=True)]
    roots_rows = [r for r in records if r["_parent"] is None]
    report["summary"] = {"logical_bytes": sum(r["logical_bytes"] for r in roots_rows),
                         "file_count": sum(r["file_count"] for r in roots_rows),
                         "category_totals": {c: {field: sum(r["category_totals"][c][field] for r in roots_rows)
                                               for field in ("logical_bytes", "file_count")} for c in CATEGORIES},
                         "directory_count": len(records), **counts,
                         "errors_omitted": max(0, counts["error_count"] - detail_limit),
                         "skipped_paths_omitted": max(0, counts["skipped_path_count"] - detail_limit),
                         "elapsed_seconds": round(time.monotonic() - started, 3)}
    report["complete"] = not report["limits_reached"] and not counts["error_count"] and all(
        r["status"] in {"scanned", "covered_by_root", "covered_by_alias", "skipped_reparse", "not_directory"}
        for r in report["roots"])
    report["snapshot_notice"] = "Live metadata snapshot, not an atomic filesystem snapshot. Reparse points and aliases are excluded."
    return report


def render_markdown(report: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    def gib(value: int) -> str:
        return f"{value / 2**30:.3f}"

    summary = report["summary"]
    lines = ["# Storage audit", "", f"Logical size: **{gib(summary['logical_bytes'])} GiB** across **{summary['file_count']:,} files**.",
             f"Coverage: {'complete within declared exclusions' if report['complete'] else 'partial; inspect errors and limits'}.", "",
             "Logical sizes are not physical disk usage or recoverable space. Hardlinks are not deduplicated.",
             "No category, filename, or age authorizes deletion. Projects, history, credentials, and unique outputs require protection.", "",
             "| Root | Logical GiB | Files | Category | Status | Newest descendant file (UTC) |",
             "|---|---:|---:|---|---|---|"]
    for root in report["roots"]:
        lines.append("| " + " | ".join(cell(v) for v in (root["path"], gib(root.get("logical_bytes", 0)),
                     root.get("file_count", 0), root["category"], root["status"], root.get("newest_descendant_file_modified") or "—")) + " |")
        # Parent and child sizes are deliberately presented separately below.
    lines.extend(["", "Directory tables contain overlapping parent/child totals; do not add them together."])
    for root in report["roots"]:
        if not root.get("top_directories"):
            continue
        lines.extend(["", f"## {cell(root['path'])}", "", "| Directory | Logical GiB | Files | Category |",
                      "|---|---:|---:|---|"])
        for row in root["top_directories"]:
            lines.append(f"| {cell(row['path'])} | {gib(row['logical_bytes'])} | {row['file_count']} | {row['category']} |")
    if report["temp_groups"]:
        lines.extend(["", "## NeuroGuide temporary-directory groups", "", "Grouping is not a deletion recommendation.", "",
                      "| Parent | Prefix | Members | Logical GiB |", "|---|---|---:|---:|"])
        for group in report["temp_groups"]:
            lines.append(f"| {cell(group['parent'])} | {cell(group['prefix'])} | {group['member_count']} | {gib(group['logical_bytes'])} |")
    lines.extend(["", "## Largest files", "", "| File | Logical GiB | Category |", "|---|---:|---|"])
    for row in report["largest_files"]:
        lines.append(f"| {cell(row['path'])} | {gib(row['logical_bytes'])} | {row['category']} |")
    lines.extend(["", f"Errors: {summary['error_count']}; skipped paths: {summary['skipped_path_count']}.",
                  "Limits reached: " + (", ".join(report["limits_reached"]) or "none") + "."])
    for row in report["errors"]:
        lines.append(f"- {cell(row['path'])}: {cell(row['type'])}: {cell(row['message'])}")
    for row in report["skipped_paths"]:
        lines.append(f"- Skipped {cell(row['path'])}: {cell(row['reason'])}")
    if summary["errors_omitted"] or summary["skipped_paths_omitted"]:
        lines.append("Additional diagnostic details were omitted by the report detail limit; counts above include them.")
    return "\n".join(lines) + "\n"


def _write_report(path: Path, text: str) -> None:
    destination = _absolute(path)
    info, blocked = _checked_stat(destination.parent)
    if blocked or info is None or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Report parent must be an existing directory without reparse ancestors.")
    # Exclusive creation prevents overwriting any existing user/state/report file.
    with destination.open("x", encoding="utf-8", newline="\n") as output:
        output.write(text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, default=[], help="Additional exact directory root; repeat as needed.")
    parser.add_argument("--only-roots", action="store_true", help="Audit only --root paths, without default discovery.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--output", type=Path, help="Explicitly create a new JSON report (never overwrite).")
    parser.add_argument("--markdown", type=Path, help="Explicitly create a new Markdown report (never overwrite).")
    parser.add_argument("--max-entries", type=int, default=500_000)
    parser.add_argument("--max-directories", type=int, default=50_000)
    parser.add_argument("--max-depth", type=int, default=64)
    parser.add_argument("--max-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    roots = ([] if args.only_roots else discover_roots()) + args.root
    if not roots:
        parser.error("Specify at least one --root with --only-roots.")
    try:
        report = audit(roots, max_entries=args.max_entries, max_directories=args.max_directories,
                       max_depth=args.max_depth, max_seconds=args.max_seconds)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        markdown = render_markdown(report)
        if args.output:
            _write_report(args.output, encoded)
        if args.markdown:
            _write_report(args.markdown, markdown)
        print(encoded if args.json else markdown, end="")
        return 0 if report["complete"] else 1
    except (OSError, ValueError) as exc:
        print(f"Storage audit error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
