#!/usr/bin/env python3
"""Read-only audit of local Codex task metadata on Windows.

The script deliberately excludes conversation bodies. It reads task metadata,
rollout file existence and size, spawn relationships, and the desktop sidebar
catalog. It never opens a SQLite database in write mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SAFE_THREAD_COLUMNS = (
    "id",
    "rollout_path",
    "created_at",
    "created_at_ms",
    "updated_at",
    "updated_at_ms",
    "recency_at",
    "recency_at_ms",
    "source",
    "thread_source",
    "cwd",
    "title",
    "name",
    "tokens_used",
    "has_user_event",
    "archived",
    "archived_at",
    "agent_nickname",
    "agent_role",
    "is_pinned",
    "project_id",
)

SAFE_CATALOG_COLUMNS = (
    "host_id",
    "thread_id",
    "display_title",
    "source_created_at",
    "source_updated_at",
    "cwd",
    "source_kind",
    "model_provider",
    "missing_candidate",
    "thread_source",
    "source_recency_at",
    "project_id",
    "conversation_origin",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of Codex task metadata, rollout files, and the "
            "Windows desktop sidebar catalog."
        )
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=codex_home / "state_5.sqlite",
        help="Path to the Codex state SQLite database.",
    )
    parser.add_argument(
        "--catalog-db",
        type=Path,
        default=codex_home / "sqlite" / "codex-dev.db",
        help="Path to the Codex desktop catalog SQLite database.",
    )
    parser.add_argument(
        "--catalog-host",
        default="local",
        help=(
            "Catalog host to compare with the local state database. The default "
            "excludes unrelated ChatGPT and remote-host entries."
        ),
    )
    parser.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="TASK_UUID",
        help="Mark a task ID as protected in the report; repeat as needed.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include helper/agent tasks in the Markdown task table.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of Markdown.",
    )
    return parser.parse_args(argv)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, table: str) -> Set[str]:
    query = "PRAGMA table_info({})".format(quote_identifier(table))
    return {str(row[1]) for row in connection.execute(query)}


def quick_check(connection: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    return "; ".join(rows)


def select_safe_rows(
    connection: sqlite3.Connection,
    table: str,
    safe_columns: Iterable[str],
    where_sql: str = "",
    parameters: Sequence[Any] = (),
) -> List[Dict[str, Any]]:
    if not table_exists(connection, table):
        return []
    existing = table_columns(connection, table)
    selected = [column for column in safe_columns if column in existing]
    if not selected:
        return []
    query = "SELECT {} FROM {}{}".format(
        ", ".join(quote_identifier(column) for column in selected),
        quote_identifier(table),
        " " + where_sql if where_sql else "",
    )
    return [dict(row) for row in connection.execute(query, parameters)]


def normalize_timestamp(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            stamp = dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        return stamp.isoformat().replace("+00:00", "Z")
    return None


def rollout_info(raw_path: Any) -> Tuple[Optional[str], bool, int]:
    if not raw_path:
        return None, False, 0
    path = Path(str(raw_path)).expanduser()
    try:
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
    except OSError:
        exists = False
        size = 0
    return str(path), exists, int(size)


def audit_state(path: Path, protected: Set[str]) -> Dict[str, Any]:
    with closing(connect_read_only(path)) as connection:
        integrity = quick_check(connection)
        raw_threads = select_safe_rows(connection, "threads", SAFE_THREAD_COLUMNS)
        edges: List[Dict[str, Any]] = []
        if table_exists(connection, "thread_spawn_edges"):
            edge_columns = table_columns(connection, "thread_spawn_edges")
            wanted = [
                name
                for name in ("parent_thread_id", "child_thread_id", "status")
                if name in edge_columns
            ]
            if "parent_thread_id" in wanted and "child_thread_id" in wanted:
                query = "SELECT {} FROM {}".format(
                    ", ".join(quote_identifier(name) for name in wanted),
                    quote_identifier("thread_spawn_edges"),
                )
                edges = [dict(row) for row in connection.execute(query)]

    parent_by_child = {
        str(edge["child_thread_id"]): str(edge["parent_thread_id"])
        for edge in edges
        if edge.get("child_thread_id") and edge.get("parent_thread_id")
    }

    threads: List[Dict[str, Any]] = []
    for raw in raw_threads:
        task_id = str(raw.get("id") or "")
        rollout_path, rollout_exists, rollout_bytes = rollout_info(
            raw.get("rollout_path")
        )
        archived = bool(raw.get("archived") or 0)
        has_user_event = bool(raw.get("has_user_event") or 0)
        pinned = bool(raw.get("is_pinned") or 0)
        thread_source = raw.get("thread_source")
        helper = bool(raw.get("agent_role") or raw.get("agent_nickname")) or (
            thread_source not in (None, "", "user")
        )
        title = raw.get("name") or raw.get("title") or "(untitled)"
        if task_id in protected:
            review_state = "protected"
        elif pinned:
            review_state = "pinned"
        elif archived:
            review_state = "archived"
        elif not rollout_exists:
            review_state = "missing-rollout"
        elif helper:
            review_state = "spawned-or-helper"
        else:
            review_state = "needs-classification"
        threads.append(
            {
                "id": task_id,
                "title": str(title),
                "cwd": raw.get("cwd"),
                "source": raw.get("thread_source") or raw.get("source"),
                "project_id": raw.get("project_id"),
                "created_at": normalize_timestamp(
                    raw.get("created_at_ms"), raw.get("created_at")
                ),
                "updated_at": normalize_timestamp(
                    raw.get("recency_at_ms"),
                    raw.get("updated_at_ms"),
                    raw.get("recency_at"),
                    raw.get("updated_at"),
                ),
                "archived": archived,
                "pinned": pinned,
                "has_user_event": has_user_event,
                "helper": helper,
                "agent_role": raw.get("agent_role"),
                "parent_thread_id": parent_by_child.get(task_id),
                "tokens_used": raw.get("tokens_used"),
                "rollout_path": rollout_path,
                "rollout_exists": rollout_exists,
                "rollout_bytes": rollout_bytes,
                "review_state": review_state,
            }
        )

    threads.sort(
        key=lambda item: (item.get("updated_at") or "", item.get("id") or ""),
        reverse=True,
    )
    return {
        "path": str(path.expanduser().resolve()),
        "quick_check": integrity,
        "threads": threads,
        "spawn_edges": edges,
    }


def audit_catalog(path: Path, catalog_host: str) -> Dict[str, Any]:
    if not path.expanduser().is_file():
        return {
            "path": str(path.expanduser()),
            "present": False,
            "quick_check": None,
            "selected_host": catalog_host,
            "all_entries_count": 0,
            "entries": [],
        }
    with closing(connect_read_only(path)) as connection:
        all_entries_count = 0
        if table_exists(connection, "local_thread_catalog"):
            all_entries_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM local_thread_catalog"
                ).fetchone()[0]
            )
        return {
            "path": str(path.expanduser().resolve()),
            "present": True,
            "quick_check": quick_check(connection),
            "selected_host": catalog_host,
            "all_entries_count": all_entries_count,
            "entries": select_safe_rows(
                connection,
                "local_thread_catalog",
                SAFE_CATALOG_COLUMNS,
                "WHERE host_id = ?",
                (catalog_host,),
            ),
        }


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    protected = {str(item).strip() for item in args.protect if str(item).strip()}
    state = audit_state(args.state_db, protected)
    catalog = audit_catalog(args.catalog_db, args.catalog_host)
    threads = state["threads"]
    state_by_id = {item["id"]: item for item in threads}

    ghost_catalog_entries: List[Dict[str, Any]] = []
    stale_archived_catalog_entries: List[Dict[str, Any]] = []
    broken_catalog_entries: List[Dict[str, Any]] = []
    for entry in catalog["entries"]:
        task_id = str(entry.get("thread_id") or "")
        state_item = state_by_id.get(task_id)
        if state_item is None:
            ghost_catalog_entries.append(entry)
        elif state_item["archived"]:
            stale_archived_catalog_entries.append(entry)
        elif not state_item["rollout_exists"]:
            broken_catalog_entries.append(entry)

    missing_rollout = [item for item in threads if not item["rollout_exists"]]
    active = [item for item in threads if not item["archived"]]
    archived = [item for item in threads if item["archived"]]
    user_tasks = [item for item in threads if not item["helper"]]
    helpers = [item for item in threads if item["helper"]]

    return {
        "schema_version": 1,
        "generated_at": dt.datetime.now(tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "read_only": True,
        "privacy": "Conversation bodies were not read.",
        "state": state,
        "catalog": catalog,
        "summary": {
            "total_state_rows": len(threads),
            "active_state_rows": len(active),
            "archived_state_rows": len(archived),
            "user_task_rows": len(user_tasks),
            "helper_or_spawned_rows": len(helpers),
            "existing_rollout_bytes": sum(
                int(item["rollout_bytes"]) for item in threads
            ),
            "state_rows_missing_rollout": len(missing_rollout),
            "catalog_rows": len(catalog["entries"]),
            "ghost_catalog_entries": len(ghost_catalog_entries),
            "stale_archived_catalog_entries": len(stale_archived_catalog_entries),
            "broken_catalog_entries": len(broken_catalog_entries),
        },
        "diagnostics": {
            "state_rows_missing_rollout": missing_rollout,
            "ghost_catalog_entries": ghost_catalog_entries,
            "stale_archived_catalog_entries": stale_archived_catalog_entries,
            "broken_catalog_entries": broken_catalog_entries,
        },
    }


def mib(byte_count: int) -> str:
    return "{:.2f} MiB".format(byte_count / (1024 * 1024))


def cell(value: Any, limit: Optional[int] = None) -> str:
    text = str(value if value is not None else "")
    text = " ".join(text.replace("|", "\\|").replace("\r", " ").splitlines())
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def markdown(report: Dict[str, Any], include_helpers: bool) -> str:
    summary = report["summary"]
    state = report["state"]
    catalog = report["catalog"]
    lines = [
        "# CodexCleaner read-only audit",
        "",
        "- Generated: `{}`".format(report["generated_at"]),
        "- State database: `{}`".format(state["path"]),
        "- State quick check: `{}`".format(state["quick_check"]),
        "- Sidebar catalog: `{}`".format(catalog["path"]),
        "- Catalog quick check: `{}`".format(catalog["quick_check"] or "not present"),
        "- Compared catalog host: `{}`".format(catalog["selected_host"]),
        "- Privacy: conversation bodies were not read; no data was changed.",
        "",
        "## Summary",
        "",
        "| Metric | Count / size |",
        "| --- | ---: |",
        "| State rows | {} |".format(summary["total_state_rows"]),
        "| Active rows | {} |".format(summary["active_state_rows"]),
        "| Archived rows | {} |".format(summary["archived_state_rows"]),
        "| User task rows | {} |".format(summary["user_task_rows"]),
        "| Helper or spawned rows | {} |".format(summary["helper_or_spawned_rows"]),
        "| Existing rollout size | {} |".format(mib(summary["existing_rollout_bytes"])),
        "| Missing rollout rows | {} |".format(summary["state_rows_missing_rollout"]),
        "| Selected local sidebar rows | {} |".format(summary["catalog_rows"]),
        "| Ghost catalog rows | {} |".format(summary["ghost_catalog_entries"]),
        "| Stale archived catalog rows | {} |".format(summary["stale_archived_catalog_entries"]),
        "| Broken catalog rows | {} |".format(summary["broken_catalog_entries"]),
        "",
        "## Tasks",
        "",
        "| State | Title | Task ID | Updated | Size | Parent task |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    displayed = 0
    for item in state["threads"]:
        if item["helper"] and not include_helpers:
            continue
        displayed += 1
        lines.append(
            "| {} | {} | `{}` | {} | {} | {} |".format(
                cell(item["review_state"]),
                cell(item["title"], 120),
                cell(item["id"]),
                cell(item["updated_at"]),
                mib(int(item["rollout_bytes"])),
                cell(item["parent_thread_id"]),
            )
        )
    if displayed == 0:
        lines.append("|  | No matching tasks |  |  |  |  |")

    diagnostics = report["diagnostics"]
    if any(diagnostics.values()):
        lines.extend(["", "## Consistency findings", ""])
        for key, entries in diagnostics.items():
            if entries:
                lines.append("- `{}`: {}".format(key, len(entries)))
    else:
        lines.extend(["", "No storage/sidebar consistency findings were detected."])

    if not include_helpers and summary["helper_or_spawned_rows"]:
        lines.extend(
            [
                "",
                "Helper and spawned rows are summarized but hidden from the table. Use `--all` to display them.",
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args(argv)
    try:
        report = build_report(args)
    except (FileNotFoundError, sqlite3.DatabaseError, OSError) as error:
        print("CodexCleaner audit failed: {}".format(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(markdown(report, args.all), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
