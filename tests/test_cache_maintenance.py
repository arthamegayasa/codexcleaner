from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
            self.assertEqual(report["target_bytes"], 24)
            self.assertFalse(backups.exists())
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
            self.assertEqual(report["moved_bytes"], 24)
            self.assertFalse((profile / "GrShaderCache").exists())
            self.assertFalse((profile / "Default" / "Cache").exists())
            self.assertTrue(
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
            self.assertEqual(len(manifest["moved"]), 2)
            self.assertTrue(all(operation["status"] == "moved" for operation in manifest["operations"]))
            self.assertTrue(manifest["space"]["same_volume_backup"])
            self.assertEqual(report["protected_before"], report["protected_after"])

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

    def test_protected_state_blocks_the_entire_preflight(self) -> None:
        names = (
            "opaque.sqlite", "opaque.sqlite3", "opaque.db", "opaque.db3",
            "opaque-wal", "opaque-shm", "opaque-journal", "WAL",
            "auth.json", "state.json", "config.toml", "router.json",
            "session_index.jsonl", "AGENTS.md", "Cookies", "Preferences",
            "nested/Local Storage/data", "nested/IndexedDB/data",
            "nested/Service Worker/data", "nested/sessions/data",
            "nested/skills/data", "nested/plugins/data", "nested/mcp/data",
            "nested/memories/data", "nested/queue/data", "nested/goals/data",
        )
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                profile, backups = self.make_fixture(Path(temp_dir))
                write_bytes(profile / "Default" / "Cache" / name, 7)
                with self.assertRaisesRegex(CACHE.MaintenanceError, "Protected"):
                    CACHE.clean_cache(profile, backups, require_stopped=False)
                self.assertFalse(backups.exists())
                self.assertTrue((profile / "GrShaderCache" / "data.bin").is_file())

    def test_sqlite_header_without_extension_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            database = profile / "Default" / "Cache" / "opaque"
            database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 128)
            with self.assertRaisesRegex(CACHE.MaintenanceError, "SQLite database"):
                CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertFalse(backups.exists())
            self.assertTrue(database.exists())

    def test_unreadable_descendant_is_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            original_scandir = CACHE.os.scandir

            def denied(path):
                if Path(path) == profile / "Default" / "Cache":
                    raise PermissionError("fixture access denied")
                return original_scandir(path)

            with mock.patch.object(CACHE.os, "scandir", side_effect=denied):
                with self.assertRaises(PermissionError):
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertFalse(backups.exists())
            self.assertTrue((profile / "GrShaderCache").exists())

    def test_any_reparse_attribute_blocks_even_when_not_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            target = profile / "GrShaderCache" / "data.bin"
            original_lstat = CACHE._lstat

            def reparse(path):
                info = original_lstat(path)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=CACHE.REPARSE_POINT)
                return info

            with mock.patch.object(CACHE, "_lstat", side_effect=reparse):
                with self.assertRaisesRegex(CACHE.MaintenanceError, "reparse"):
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertFalse(backups.exists())
            self.assertTrue(target.exists())

    def test_all_moves_have_persisted_write_ahead_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            original_rename = CACHE.os.rename
            recorded = []

            def checked_rename(source, destination):
                manifest = json.loads((Path(destination).parent / "manifest.json").read_text(encoding="utf-8"))
                operation = next(item for item in manifest["operations"] if item["source_path"] == str(source))
                self.assertEqual(operation["status"], "prepared")
                self.assertEqual(operation["backup_path"], str(destination))
                self.assertEqual(len(manifest["operations"]), 2)
                recorded.append(str(source))
                return original_rename(source, destination)

            with mock.patch.object(CACHE.os, "rename", side_effect=checked_rename):
                report = CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertEqual(len(recorded), 2)
            self.assertEqual(report["moved_bytes"], 24)

    def test_failed_second_move_keeps_first_backup_and_records_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            original_rename = CACHE.os.rename

            def fail_second(source, destination):
                if Path(source).name == "Cache":
                    raise PermissionError("fixture lock")
                return original_rename(source, destination)

            with mock.patch.object(CACHE.os, "rename", side_effect=fail_second):
                with self.assertRaises(CACHE.PartialCleanupError) as caught:
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            report = caught.exception.report
            manifest = json.loads((Path(report["backup_dir"]) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(manifest["moved_bytes"], 11)
            self.assertEqual([item["status"] for item in manifest["operations"]], ["moved", "failed"])
            self.assertTrue(manifest["operations"][1]["source_present"])
            self.assertFalse(manifest["operations"][1]["backup_present"])
            self.assertTrue((profile / "Default" / "Cache" / "http.bin").is_file())
            self.assertTrue((Path(report["backup_dir"]) / "GrShaderCache" / "data.bin").is_file())

    def test_initial_manifest_failure_prevents_every_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            with mock.patch.object(CACHE, "_write_manifest", side_effect=OSError("fixture disk full")):
                with self.assertRaises(CACHE.PartialCleanupError) as caught:
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertEqual(caught.exception.report["moved_bytes"], 0)
            self.assertTrue((profile / "GrShaderCache" / "data.bin").is_file())
            self.assertTrue((profile / "Default" / "Cache" / "http.bin").is_file())

    def test_post_move_journal_failure_retains_recovery_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            original_write = CACHE._write_manifest

            def fail_after_move(backup_dir, report):
                if report["moved"]:
                    raise OSError("fixture journal unavailable")
                return original_write(backup_dir, report)

            with mock.patch.object(CACHE, "_write_manifest", side_effect=fail_after_move):
                with self.assertRaises(CACHE.PartialCleanupError) as caught:
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            report = caught.exception.report
            manifest = json.loads((Path(report["backup_dir"]) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["operations"][0]["status"], "prepared")
            self.assertEqual(report["moved_bytes"], 11)
            self.assertIn("manifest_warning", report)
            self.assertTrue((profile / "Default" / "Cache").exists())
            self.assertTrue((Path(report["backup_dir"]) / "GrShaderCache" / "data.bin").exists())

    def test_atomic_manifest_failure_keeps_previous_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            CACHE._write_manifest(root, {"status": "prepared"})
            with mock.patch.object(CACHE.os, "replace", side_effect=PermissionError("fixture locked manifest")):
                with self.assertRaises(PermissionError):
                    CACHE._write_manifest(root, {"status": "moved"})
            self.assertEqual(json.loads((root / "manifest.json").read_text()), {"status": "prepared"})
            self.assertEqual([item.name for item in root.iterdir()], ["manifest.json"])

    def test_process_restart_after_journal_blocks_the_first_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            running = mock.Mock(side_effect=[False, False, False, True])
            with self.assertRaises(CACHE.PartialCleanupError) as caught:
                CACHE.clean_cache(profile, backups, running_check=running)
            report = caught.exception.report
            self.assertEqual(report["moved_bytes"], 0)
            self.assertEqual(report["failures"][0]["stage"], "final preflight")
            self.assertTrue((profile / "GrShaderCache").exists())

    def test_target_change_after_write_ahead_blocks_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            original_write = CACHE._write_manifest

            def add_state_after_journal(backup_dir, report):
                original_write(backup_dir, report)
                if report["operations"][0]["status"] == "prepared":
                    write_bytes(profile / "GrShaderCache" / "auth.json", 3)

            with mock.patch.object(CACHE, "_write_manifest", side_effect=add_state_after_journal):
                with self.assertRaises(CACHE.PartialCleanupError) as caught:
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertEqual(caught.exception.report["moved_bytes"], 0)
            self.assertTrue((profile / "GrShaderCache" / "auth.json").exists())

    def test_observed_free_space_is_separate_from_moved_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            disk = SimpleNamespace(total=1000000, used=600000, free=400000)
            with mock.patch.object(CACHE.shutil, "disk_usage", return_value=disk):
                report = CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertEqual(report["moved_bytes"], 24)
            self.assertEqual(report["space"]["observed_profile_free_delta"], 0)
            output = io.StringIO()
            with redirect_stdout(output):
                CACHE.print_cleanup(report)
            self.assertIn("do not represent reclaimed disk space", output.getvalue())

    def test_cross_volume_backup_is_rejected_before_copy_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            with mock.patch.object(CACHE, "_space_before", return_value={"same_volume_backup": False}):
                with self.assertRaisesRegex(CACHE.MaintenanceError, "same volume"):
                    CACHE.clean_cache(profile, backups, require_stopped=False)
            self.assertFalse(backups.exists())
            self.assertTrue((profile / "GrShaderCache").exists())

    def test_cli_without_apply_does_not_mutate_and_has_no_process_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, backups = self.make_fixture(Path(temp_dir))
            with redirect_stderr(io.StringIO()):
                code = CACHE.main(["clean", "--profile-root", str(profile), "--backup-root", str(backups)])
            self.assertEqual(code, 2)
            self.assertFalse(backups.exists())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                CACHE.build_parser().parse_args(["clean", "--apply", "--no-require-stopped"])


class ProcessVerificationTests(unittest.TestCase):
    def query(self, output, returncode=0, stderr=""):
        result = SimpleNamespace(stdout=output, stderr=stderr, returncode=returncode)
        with mock.patch.object(CACHE.os, "name", "nt"), mock.patch.object(CACHE.subprocess, "run", return_value=result):
            return CACHE.chatgpt_running()

    def test_recognizes_desktop_cli_and_helper_names_case_insensitively(self) -> None:
        for name in ("ChatGPT.exe", "Codex.exe", "CODEX.EXE", "codex-command-runner.exe", "codex-cli.exe", "Codex Helper.exe"):
            with self.subTest(name=name):
                self.assertTrue(self.query('"{}","42","Console","1","12,345 K"\n'.format(name)))

    def test_valid_complete_listing_without_codex_is_stopped(self) -> None:
        self.assertFalse(self.query('"System Idle Process","0","Services","0","8 K"\n"explorer.exe","42","Console","1","100 K"\n'))

    def test_tasklist_failures_never_count_as_stopped(self) -> None:
        cases = (
            ("", 0, ""), ("INFO: No tasks are running which match the specified criteria.", 0, ""),
            ("ERROR: Access is denied.", 1, ""), ('"explorer.exe","42","Console","1","100 K"', 1, ""),
            ('"explorer.exe","42","Console","1","100 K"', 0, "ERROR: query incomplete"),
            ('"bad.csv","NaN","Console","1","100 K"', 0, ""),
            ('"explorer.exe","42","Console","1","100 K"\nERROR: query incomplete', 0, ""),
            ('"unterminated', 0, ""),
        )
        for output, code, stderr in cases:
            with self.subTest(output=output), self.assertRaises(CACHE.MaintenanceError):
                self.query(output, code, stderr)

    def test_process_query_exceptions_block_cleanup(self) -> None:
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("tasklist", 15), UnicodeError("decode")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(CACHE.os, "name", "nt"), mock.patch.object(CACHE.subprocess, "run", side_effect=error):
                    with self.assertRaises(CACHE.MaintenanceError):
                        CACHE.chatgpt_running()


@unittest.skipUnless(os.name == "nt", "real NTFS junction tests require Windows")
class WindowsJunctionTests(unittest.TestCase):
    make_fixture = CacheMaintenanceTests.make_fixture

    @contextmanager
    def junction(self, link: Path, target: Path):
        link.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        try:
            self.assertTrue(link.lstat().st_file_attributes & CACHE.REPARSE_POINT)
            yield
        finally:
            # Remove only the fixture junction itself, never its target tree.
            os.rmdir(link)

    def test_profile_root_and_profile_ancestor_junctions_are_rejected(self) -> None:
        for use_ancestor in (False, True):
            with self.subTest(ancestor=use_ancestor), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                profile, backups = self.make_fixture(root / "actual")
                target = profile.parent if use_ancestor else profile
                with self.junction(root / "alias", target):
                    selected = root / "alias" / "profile" if use_ancestor else root / "alias"
                    with self.assertRaisesRegex(CACHE.MaintenanceError, "reparse"):
                        CACHE.clean_cache(selected, backups, require_stopped=False)
                    self.assertFalse(backups.exists())
                    self.assertTrue((profile / "GrShaderCache" / "data.bin").exists())

    def test_target_and_nested_junctions_are_rejected_without_any_moves(self) -> None:
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                profile, backups = self.make_fixture(root)
                external = root / "external"
                write_bytes(external / "keep.bin", 37)
                link = profile / "GrShaderCache" / "nested" if nested else profile / "ShaderCache"
                with self.junction(link, external):
                    with self.assertRaisesRegex(CACHE.MaintenanceError, "reparse"):
                        CACHE.clean_cache(profile, backups, require_stopped=False)
                    self.assertFalse(backups.exists())
                    self.assertEqual((external / "keep.bin").read_bytes(), b"x" * 37)
                    self.assertTrue((profile / "GrShaderCache" / "data.bin").exists())

    def test_backup_ancestor_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile, _ = self.make_fixture(root)
            target = root / "actual-backups"
            target.mkdir()
            with self.junction(root / "backups-alias", target):
                with self.assertRaisesRegex(CACHE.MaintenanceError, "reparse"):
                    CACHE.clean_cache(profile, root / "backups-alias" / "nested", require_stopped=False)
                self.assertEqual(list(target.iterdir()), [])
                self.assertTrue((profile / "GrShaderCache").exists())

    def test_junction_inserted_after_journal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile, backups = self.make_fixture(root)
            external = root / "external"
            write_bytes(external / "keep.bin", 37)
            link = profile / "GrShaderCache" / "inserted"
            original_write = CACHE._write_manifest
            junction_context = None

            def add_junction(backup_dir, report):
                nonlocal junction_context
                original_write(backup_dir, report)
                if junction_context is None and report["operations"][0]["status"] == "prepared":
                    junction_context = self.junction(link, external)
                    junction_context.__enter__()

            try:
                with mock.patch.object(CACHE, "_write_manifest", side_effect=add_junction):
                    with self.assertRaises(CACHE.PartialCleanupError) as caught:
                        CACHE.clean_cache(profile, backups, require_stopped=False)
                self.assertEqual(caught.exception.report["moved_bytes"], 0)
                self.assertEqual((external / "keep.bin").read_bytes(), b"x" * 37)
            finally:
                if junction_context is not None:
                    junction_context.__exit__(None, None, None)


@unittest.skipUnless(os.name == "nt", "batch launcher tests require Windows")
class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_defaults_to_audit_and_requires_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "fixture scripts"
            root.mkdir()
            launcher = root / "run_cache_cleanup.cmd"
            shutil.copyfile(SKILL_ROOT / "scripts" / launcher.name, launcher)
            # The launcher invokes this harmless fixture, never the real helper.
            (root / "cache_maintenance.py").write_text(
                'import sys, json\nprint("FIXTURE_ARGS=" + json.dumps(sys.argv[1:]))\n',
                encoding="utf-8",
            )
            for arguments, expected in (([], ["audit"]), (["--apply"], ["clean", "--apply", "--relaunch"])):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        ["cmd.exe", "/d", "/c", str(launcher), *arguments],
                        input="\n", capture_output=True, text=True, errors="replace",
                        cwd=root, timeout=15, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("FIXTURE_ARGS=" + json.dumps(expected), result.stdout)
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(launcher), "--unknown"],
                capture_output=True, text=True, errors="replace", cwd=root, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("FIXTURE_ARGS=", result.stdout)


if __name__ == "__main__":
    unittest.main()
