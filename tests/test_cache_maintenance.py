from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_ROOT / "scripts" / "cache_maintenance.py"
SPEC = importlib.util.spec_from_file_location("cache_maintenance", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load cache_maintenance.py")
CACHE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CACHE
SPEC.loader.exec_module(CACHE)


def write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class CacheMaintenanceTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path]:
        profile = root / "profile"
        backups = root / "backups"
        write_bytes(profile / "GrShaderCache" / "data.bin", 11)
        write_bytes(profile / "Default" / "Cache" / "http.bin", 13)
        write_bytes(
            profile
            / "Default"
            / "Partitions"
            / "codex-browser-app"
            / "Service Worker"
            / "CacheStorage"
            / "sw.bin",
            17,
        )
        write_bytes(profile / "Default" / "Network" / "Cookies", 19)
        write_bytes(profile / "Default" / "Local Storage" / "auth.bin", 23)
        write_bytes(profile / "Default" / "IndexedDB" / "state.bin", 29)
        write_bytes(profile / "unrelated" / "keep.bin", 31)
        return profile, backups

    def test_audit_is_read_only_and_reports_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            report = CACHE.audit_cache(profile, backups)
            self.assertTrue(report["read_only"])
            self.assertEqual(report["target_bytes"], 41)
            self.assertTrue((profile / "GrShaderCache" / "data.bin").is_file())
            self.assertTrue((profile / "Default" / "Network" / "Cookies").is_file())

    def test_clean_moves_only_allowlisted_cache_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            report = CACHE.clean_cache(
                profile,
                backups,
                require_stopped=False,
                now=dt.datetime(2026, 8, 22, 18, 0, 0),
            )
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["moved_bytes"], 41)
            self.assertFalse((profile / "GrShaderCache").exists())
            self.assertFalse((profile / "Default" / "Cache").exists())
            self.assertFalse(
                (
                    profile
                    / "Default"
                    / "Partitions"
                    / "codex-browser-app"
                    / "Service Worker"
                ).exists()
            )
            self.assertTrue((profile / "Default" / "Network" / "Cookies").is_file())
            self.assertTrue((profile / "Default" / "Local Storage" / "auth.bin").is_file())
            self.assertTrue((profile / "Default" / "IndexedDB" / "state.bin").is_file())
            self.assertTrue((profile / "unrelated" / "keep.bin").is_file())
            backup_dir = backups / "20260822-180000"
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(len(manifest["moved"]), 3)

    def test_clean_refuses_when_app_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            with self.assertRaises(CACHE.AppRunningError):
                CACHE.clean_cache(
                    profile,
                    backups,
                    running_check=lambda: True,
                    wait_seconds=0,
                )
            self.assertFalse(backups.exists())
            self.assertTrue((profile / "GrShaderCache" / "data.bin").is_file())

    def test_clean_refuses_backup_inside_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _ = self.make_fixture(Path(temp_dir))
            with self.assertRaises(CACHE.MaintenanceError):
                CACHE.clean_cache(
                    profile,
                    profile / "backups",
                    require_stopped=False,
                )


if __name__ == "__main__":
    unittest.main()
