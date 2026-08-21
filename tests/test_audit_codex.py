from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_ROOT / "scripts" / "audit_codex.py"
SPEC = importlib.util.spec_from_file_location("audit_codex", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load audit_codex.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class AuditCodexTests(unittest.TestCase):
    def test_local_catalog_and_rollout_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_db = root / "state.sqlite"
            catalog_db = root / "catalog.sqlite"
            active_rollout = root / "active.jsonl"
            archived_rollout = root / "archived.jsonl"
            helper_rollout = root / "helper.jsonl"
            active_rollout.write_text("active", encoding="utf-8")
            archived_rollout.write_text("archived", encoding="utf-8")
            helper_rollout.write_text("helper", encoding="utf-8")

            with closing(sqlite3.connect(state_db)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        rollout_path TEXT,
                        updated_at_ms INTEGER,
                        thread_source TEXT,
                        title TEXT,
                        archived INTEGER,
                        is_pinned INTEGER,
                        has_user_event INTEGER,
                        agent_role TEXT,
                        agent_nickname TEXT
                    );
                    CREATE TABLE thread_spawn_edges (
                        parent_thread_id TEXT,
                        child_thread_id TEXT PRIMARY KEY,
                        status TEXT
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "active",
                            str(active_rollout),
                            1_700_000_000_000,
                            "user",
                            "Active task",
                            0,
                            0,
                            0,
                            None,
                            None,
                        ),
                        (
                            "archived",
                            str(archived_rollout),
                            1_699_000_000_000,
                            "user",
                            "Archived task",
                            1,
                            0,
                            0,
                            None,
                            None,
                        ),
                        (
                            "helper",
                            str(helper_rollout),
                            1_698_000_000_000,
                            "subagent",
                            "Helper task",
                            0,
                            0,
                            0,
                            None,
                            None,
                        ),
                        (
                            "missing",
                            str(root / "missing.jsonl"),
                            1_697_000_000_000,
                            "user",
                            "Missing rollout",
                            0,
                            0,
                            0,
                            None,
                            None,
                        ),
                    ],
                )
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                    ("active", "helper", "completed"),
                )
                connection.commit()

            with closing(sqlite3.connect(catalog_db)) as connection:
                connection.execute(
                    """
                    CREATE TABLE local_thread_catalog (
                        host_id TEXT,
                        thread_id TEXT,
                        display_title TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?)",
                    [
                        ("local", "active", "Active task"),
                        ("local", "archived", "Archived task"),
                        ("local", "missing", "Missing rollout"),
                        ("local", "ghost", "Ghost task"),
                        ("chatgpt:example", "remote", "Remote conversation"),
                    ],
                )
                connection.commit()

            args = argparse.Namespace(
                state_db=state_db,
                catalog_db=catalog_db,
                catalog_host="local",
                protect=["active"],
                all=False,
                json=False,
            )
            report = AUDIT.build_report(args)

            self.assertEqual(report["state"]["quick_check"], "ok")
            self.assertEqual(report["catalog"]["quick_check"], "ok")
            self.assertEqual(report["catalog"]["all_entries_count"], 5)
            self.assertEqual(report["summary"]["catalog_rows"], 4)
            self.assertEqual(report["summary"]["user_task_rows"], 3)
            self.assertEqual(report["summary"]["helper_or_spawned_rows"], 1)
            self.assertEqual(report["summary"]["state_rows_missing_rollout"], 1)
            self.assertEqual(report["summary"]["ghost_catalog_entries"], 1)
            self.assertEqual(report["summary"]["stale_archived_catalog_entries"], 1)
            self.assertEqual(report["summary"]["broken_catalog_entries"], 1)
            self.assertEqual(
                {item["thread_id"] for item in report["diagnostics"]["ghost_catalog_entries"]},
                {"ghost"},
            )
            self.assertEqual(
                next(item for item in report["state"]["threads"] if item["id"] == "active")[
                    "review_state"
                ],
                "protected",
            )
            json.dumps(report)


if __name__ == "__main__":
    unittest.main()
