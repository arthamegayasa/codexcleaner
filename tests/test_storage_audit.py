from __future__ import annotations

from contextlib import contextmanager, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "storage_audit.py"
SPEC = importlib.util.spec_from_file_location("storage_audit", MODULE)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def fake_stat(original, **changes):
    fields = {name: getattr(original, name) for name in dir(original) if name.startswith("st_")}
    fields.update(changes)
    return SimpleNamespace(**fields)


class StorageAuditTests(unittest.TestCase):
    def test_hidden_files_sizes_and_newest_descendant_without_content_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / ".hidden" / "deep"
            child.mkdir(parents=True)
            (root / "first").write_bytes(b"123")
            leaf = child / ".private"
            leaf.write_bytes(b"12345")
            os.utime(root / "first", (1_600_000_000, 1_600_000_000))
            os.utime(leaf, (1_700_000_000, 1_700_000_000))
            # Directory times must not be substituted for the latest file time.
            os.utime(child, (1_800_000_000, 1_800_000_000))
            with mock.patch("builtins.open", side_effect=AssertionError("File content read")), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("File content read")):
                report = AUDIT.audit([root])
            self.assertTrue(report["complete"])
            self.assertEqual(report["summary"]["logical_bytes"], 8)
            self.assertEqual(report["summary"]["file_count"], 2)
            row = report["roots"][0]
            self.assertEqual(row["newest_descendant_file_modified"], "2023-11-14T22:13:20Z")
            self.assertEqual(row["immediate_directories"][0]["file_count"], 1)
            self.assertEqual(row["directory_count"], 2)
            self.assertEqual(len(report["largest_files"]), 2)

    def test_overlapping_roots_are_counted_once_even_when_child_is_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            child = parent / "child"
            child.mkdir()
            (child / "a").write_bytes(b"a" * 7)
            (parent / "b").write_bytes(b"b" * 3)
            report = AUDIT.audit([child, parent, parent])
            self.assertEqual(report["summary"]["logical_bytes"], 10)
            self.assertEqual(report["summary"]["file_count"], 2)
            self.assertEqual([r["status"] for r in report["roots"]],
                             ["covered_by_root", "scanned", "covered_by_root"])

    def test_reparse_ancestor_prevents_scandir_of_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            ancestor = Path(temporary) / "junction"
            requested = ancestor / "child"
            requested.mkdir(parents=True)
            actual_lstat = os.lstat

            def marked(path, *args, **kwargs):
                value = actual_lstat(path, *args, **kwargs)
                if Path(path) == ancestor:
                    return fake_stat(value, st_file_attributes=AUDIT.REPARSE_POINT)
                return value

            with mock.patch.object(AUDIT.os, "lstat", side_effect=marked), \
                    mock.patch.object(AUDIT.os, "scandir", side_effect=AssertionError("Traversed reparse ancestor")):
                report = AUDIT.audit([requested])
            self.assertEqual(report["roots"][0]["status"], "skipped_reparse")
            self.assertEqual(report["summary"]["file_count"], 0)
            self.assertEqual(report["skipped_paths"][0]["blocked_path"], str(ancestor))

    def test_reparse_entry_is_skipped_without_descending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_entry = SimpleNamespace(path=str(root / "junction"),
                stat=lambda **kwargs: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
            context = mock.MagicMock()
            context.__enter__.return_value = iter([fake_entry])
            with mock.patch.object(AUDIT.os, "scandir", return_value=context) as scanned:
                report = AUDIT.audit([root])
            self.assertEqual(scanned.call_count, 1)
            self.assertEqual(report["skipped_paths"][0]["reason"], "reparse_or_symlink")

    def test_real_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            root = base / "root"
            target.mkdir(); root.mkdir()
            (target / "keep").write_bytes(b"not in root")
            link = root / "linked"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlink creation is unavailable: {exc}")
            report = AUDIT.audit([root])
            self.assertEqual(report["summary"]["logical_bytes"], 0)
            self.assertEqual(report["summary"]["skipped_path_count"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows junction integration test")
    def test_real_windows_junction_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            root = base / "root"
            target.mkdir(); root.mkdir()
            (target / "keep").write_bytes(b"not in root")
            junction = root / "junction"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                self.skipTest("Junction creation unavailable in test environment")
            try:
                report = AUDIT.audit([root, junction / "nested"])
                self.assertEqual(report["summary"]["logical_bytes"], 0)
                self.assertEqual(report["roots"][1]["status"], "skipped_reparse")
                self.assertEqual(report["summary"]["error_count"], 0)
            finally:
                # rmdir unlinks this test junction itself; target is preserved.
                os.rmdir(junction)
            self.assertTrue((target / "keep").exists())

    def test_permission_errors_are_visible_and_coverage_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocked = root / "blocked"
            blocked.mkdir()
            (root / "visible").write_bytes(b"1234")
            actual_scandir = os.scandir

            def denied(path):
                if Path(path) == blocked:
                    raise PermissionError("test access denied")
                return actual_scandir(path)

            with mock.patch.object(AUDIT.os, "scandir", side_effect=denied):
                report = AUDIT.audit([root])
            self.assertFalse(report["complete"])
            self.assertEqual(report["roots"][0]["status"], "partial")
            self.assertEqual(report["summary"]["logical_bytes"], 4)
            self.assertEqual(report["errors"][0]["type"], "PermissionError")
            self.assertEqual(report["errors"][0]["path"], str(blocked))

    def test_missing_root_is_explicit_and_does_not_abort_other_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "present"
            root.mkdir()
            (root / "a").write_bytes(b"yes")
            report = AUDIT.audit([base / "missing", root])
            self.assertEqual(report["roots"][0]["status"], "missing")
            self.assertEqual(report["summary"]["logical_bytes"], 3)
            self.assertFalse(report["complete"])

    def test_directory_identity_aliases_and_zero_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first, second = base / "first", base / "second"
            first.mkdir(); second.mkdir()
            (first / "a").write_bytes(b"123")
            (second / "b").write_bytes(b"123")
            actual_lstat = os.lstat

            def same_identity(path, *args, **kwargs):
                value = actual_lstat(path, *args, **kwargs)
                return fake_stat(value, st_dev=42, st_ino=50) if Path(path) in {first, second} else value

            with mock.patch.object(AUDIT.os, "lstat", side_effect=same_identity):
                report = AUDIT.audit([first, second])
            self.assertEqual(report["summary"]["logical_bytes"], 3)
            self.assertEqual(report["roots"][1]["status"], "covered_by_alias")
            self.assertIsNone(AUDIT._identity(SimpleNamespace(st_dev=0, st_ino=0)))

    def test_state_and_artifacts_cannot_be_misclassified_as_cache(self):
        paths = {
            r"C:\Users\A\.codex\archived_sessions\old.jsonl": "protected_state",
            r"C:\Users\A\.codex\sessions\old.jsonl": "protected_state",
            r"C:\Users\A\.codex\cache\state_5.sqlite-wal": "protected_state",
            r"C:\Users\A\.codex\tmp\auth.json": "protected_state",
            r"C:\Users\A\.codex\config.toml": "protected_state",
            r"C:\Users\A\.codex\codex-router\requests.json": "protected_state",
            r"C:\Users\A\.codex\generated_images\old.png": "review_artifact",
            r"C:\Users\A\.codex\visualizations\chart.html": "review_artifact",
            r"C:\Users\A\.codex\worktrees\x\node_modules\a": "project",
            r"C:\Users\A\Documents\Codex\old\work\code.py": "project",
            r"C:\Users\A\.cache\codex-runtimes\codex-primary-runtime\bin": "protected_state",
            r"C:\Users\A\.cache\codex-runtimes\codex-runtime-install-ab1234\archive": "cache_candidate",
            r"C:\Users\A\.codex\cache\catalog.json": "cache_candidate",
            r"C:\Users\A\Temp\neuroguide-report-demo\retained-report.pdf": "review_artifact",
            r"C:\Users\A\Temp\NeuroGuide_Research_ab12cd34\nested\opaque.bin": "review_artifact",
            r"C:\Users\A\Temp\neuroguide-report-demo\results.sqlite": "protected_state",
            r"C:\Users\A\Unsorted\notes.txt": "unknown",
        }
        for path, category in paths.items():
            with self.subTest(path=path):
                self.assertEqual(AUDIT.classify_path(Path(path))[0], category)

    def test_neuroguide_report_bundles_and_groups_stay_in_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Temp"
            for name in ("neuroguide-report-ab12cd34", "neuroguide-report-ef56ab78",
                         "NeuroGuide_Research_ab12cd34"):
                bundle = root / name / "nested"
                bundle.mkdir(parents=True)
                (bundle / "retained-report.pdf").write_bytes(b"%PDF fixture output")
                (bundle / "opaque.bin").write_bytes(b"unique research artifact")
            with mock.patch("builtins.open", side_effect=AssertionError("File content read")), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("File content read")):
                report = AUDIT.audit([root])
            self.assertTrue(report["complete"])
            self.assertEqual(report["summary"]["category_totals"]["review_artifact"]["file_count"], 6)
            self.assertEqual(report["summary"]["category_totals"]["cache_candidate"]["file_count"], 0)
            rows = (report["roots"][0]["top_directories"] + report["largest_files"] + report["temp_groups"])
            self.assertEqual(len(report["temp_groups"]), 2)
            for row in rows:
                self.assertEqual(row["category"], "review_artifact")
                self.assertFalse(row["deletion_allowed"])

    def test_late_project_marker_protects_own_files_descendants_and_not_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Temp"
            project = root / "repo-alpha"
            for child in (project / "build", project / "node_modules" / "library", project / ".git"):
                child.mkdir(parents=True)
            (project / "source.py").write_bytes(b"source")
            (project / "build" / "old.bin").write_bytes(b"future build")
            (project / "node_modules" / "library" / "index.js").write_bytes(b"dependency")
            (project / ".git" / "HEAD").write_bytes(b"repository metadata")
            (project / "auth.json").write_bytes(b"state")
            sibling = root / "cache"
            sibling.mkdir()
            (sibling / "compiled.bin").write_bytes(b"cache")
            original_scandir = os.scandir

            @contextmanager
            def marker_last(path):
                with original_scandir(path) as entries:
                    ordered = sorted(entries, key=lambda entry: (entry.name == ".git", entry.name))
                yield iter(ordered)

            with mock.patch.object(AUDIT.os, "scandir", side_effect=marker_last), \
                    mock.patch("builtins.open", side_effect=AssertionError("File content read")), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("File content read")):
                report = AUDIT.audit([root])
            self.assertTrue(report["complete"])
            files = {Path(row["path"]): row for row in report["largest_files"]}
            for path in (project / "source.py", project / "build" / "old.bin",
                         project / "node_modules" / "library" / "index.js"):
                self.assertEqual(files[path]["category"], "project")
                self.assertIn(str(project), files[path]["classification_reason"])
            for path in (project / ".git" / "HEAD", project / "auth.json"):
                self.assertEqual(files[path]["category"], "protected_state")
            self.assertEqual(files[sibling / "compiled.bin"]["category"], "cache_candidate")
            directories = {Path(row["path"]): row for row in report["roots"][0]["top_directories"]}
            for path in (project, project / "build", project / "node_modules", project / "node_modules" / "library"):
                self.assertEqual(directories[path]["category"], "project")
            totals = report["summary"]["category_totals"]
            self.assertEqual(totals["project"]["file_count"], 3)
            self.assertEqual(totals["protected_state"]["file_count"], 2)
            self.assertEqual(totals["cache_candidate"]["file_count"], 1)
            for row in [*files.values(), *directories.values(), *report["roots"]]:
                self.assertFalse(row["deletion_allowed"])

    def test_project_root_and_nested_root_recognize_worktree_pointer_without_reading_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "Temp" / "worktree-alpha"
            child = project / "build"
            child.mkdir(parents=True)
            (project / ".git").write_bytes(b"gitdir: nonexistent path must never be opened\n")
            (child / "old.bin").write_bytes(b"retained future work")
            for selected in (project, child):
                with self.subTest(selected=selected), \
                        mock.patch("builtins.open", side_effect=AssertionError("File content read")), \
                        mock.patch.object(Path, "open", side_effect=AssertionError("File content read")):
                    report = AUDIT.audit([selected])
                self.assertTrue(report["complete"])
                self.assertEqual(report["roots"][0]["category"], "project")
                file = next(row for row in report["largest_files"] if Path(row["path"]) == child / "old.bin")
                self.assertEqual(file["category"], "project")
                self.assertFalse(file["deletion_allowed"])

    def test_non_git_project_manifest_sets_a_project_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Temp"
            project = root / "app-alpha"
            child = project / "build"
            child.mkdir(parents=True)
            (project / "package.json").write_bytes(b"fixture: not valid JSON and must not be parsed")
            (child / "old.bin").write_bytes(b"future reuse")
            with mock.patch("builtins.open", side_effect=AssertionError("File content read")), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("File content read")):
                report = AUDIT.audit([root])
            self.assertTrue(report["complete"])
            self.assertEqual(report["summary"]["category_totals"]["project"]["file_count"], 2)
            self.assertTrue(all(row["category"] == "project" for row in report["largest_files"]))

    def test_sparse_file_length_is_never_labeled_as_recoverable_space(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (root / "sparse.bin").open("wb") as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b"x")
            report = AUDIT.audit([root])
            self.assertEqual(report["summary"]["logical_bytes"], 16 * 1024 * 1024 + 1)
            self.assertEqual(report["measurement"]["kind"], "logical_file_size")
            self.assertIsNone(report["measurement"]["physical_bytes"])
            self.assertIsNone(report["measurement"]["recoverable_bytes"])
            self.assertFalse(report["largest_files"][0]["deletion_allowed"])
            self.assertIn("not physical", AUDIT.render_markdown(report))

    def test_protected_descendants_are_exposed_even_in_a_cache_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".codex" / "cache"
            root.mkdir(parents=True)
            (root / "auth.json").write_bytes(b"private")
            (root / "catalog.json").write_bytes(b"cache")
            report = AUDIT.audit([root], largest_limit=1)
            totals = report["roots"][0]["category_totals"]
            self.assertEqual(totals["protected_state"]["logical_bytes"], 7)
            self.assertEqual(totals["cache_candidate"]["logical_bytes"], 5)
            self.assertFalse(report["roots"][0]["deletion_allowed"])
            self.assertEqual(report["summary"]["category_totals"], totals)

    def test_nonfinite_or_invalid_limits_are_rejected_without_scanning(self):
        with mock.patch.object(AUDIT.os, "scandir", side_effect=AssertionError("Should not scan")):
            for seconds in (float("nan"), float("inf"), 0, -1):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    AUDIT.audit([], max_seconds=seconds)

    def test_limits_produce_partial_reports_and_do_not_overrun_entry_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for i in range(8):
                (root / str(i)).write_bytes(b"a")
            report = AUDIT.audit([root], max_entries=3)
            self.assertFalse(report["complete"])
            self.assertEqual(report["summary"]["entries_examined"], 3)
            self.assertEqual(report["summary"]["file_count"], 3)
            self.assertIn("max_entries", report["limits_reached"])
            child = root / "child"
            child.mkdir()
            (child / "nested").write_bytes(b"not counted")
            shallow = AUDIT.audit([root], max_depth=0)
            self.assertEqual(shallow["summary"]["file_count"], 8)
            self.assertIn("max_depth", shallow["limits_reached"])

    def test_neuroguide_prefix_groups_preserve_meaning_and_never_authorize_delete(self):
        self.assertEqual(AUDIT.stable_temp_prefix("NeuroGuide_Portable_ab12cd34"), "NeuroGuide_Portable")
        self.assertEqual(AUDIT.stable_temp_prefix("NeuroGuide_Portable_20260921_153012_ab12cd34"), "NeuroGuide_Portable")
        self.assertEqual(AUDIT.stable_temp_prefix("NeuroGuide_Portable"), "NeuroGuide_Portable")
        self.assertEqual(AUDIT.stable_temp_prefix("NeuroGuide_v20260922"), "NeuroGuide_v20260922")
        self.assertIsNone(AUDIT.stable_temp_prefix("SomethingElse_abc12345"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Temp"
            for suffix in ("ab12cd34", "ef56ab78"):
                child = root / ("NeuroGuide_Portable_" + suffix)
                child.mkdir(parents=True)
                (child / "file").write_bytes(b"1234")
            report = AUDIT.audit([root])
            self.assertEqual(len(report["temp_groups"]), 1)
            group = report["temp_groups"][0]
            self.assertEqual(group["member_count"], 2)
            self.assertEqual(group["logical_bytes"], 8)
            self.assertFalse(group["deletion_allowed"])

    def test_cli_only_scans_explicit_test_root_and_reports_are_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            (root / "data").write_bytes(b"123")
            output = base / "report.json"
            markdown = base / "report.md"
            stdout = io.StringIO()
            with mock.patch.object(AUDIT, "discover_roots", side_effect=AssertionError("Real roots discovered")), redirect_stdout(stdout):
                code = AUDIT.main(["--only-roots", "--root", str(root), "--json", "--output", str(output), "--markdown", str(markdown)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["summary"]["logical_bytes"], 3)
            original = output.read_bytes()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = AUDIT.main(["--only-roots", "--root", str(root), "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(output.read_bytes(), original)
            self.assertTrue(markdown.exists())

    def test_discovery_is_metadata_free_and_uses_configured_profile_paths(self):
        environment = {"USERPROFILE": "C:/Users/TestUser", "LOCALAPPDATA": "C:/Users/TestUser/AppData/Local",
                       "APPDATA": "C:/Users/TestUser/AppData/Roaming", "CODEX_HOME": "D:/ConfiguredCodex"}
        with mock.patch.dict(os.environ, environment, clear=True):
            roots = [str(p).replace("\\", "/") for p in AUDIT.discover_roots()]
        self.assertIn("C:/Users/TestUser/.codex", roots)
        self.assertIn("C:/Users/TestUser/AppData/Local/Temp", roots)
        self.assertIn("C:/Users/TestUser/.cache/codex-runtimes", roots)
        self.assertIn("C:/Users/TestUser/Documents/Codex", roots)
        self.assertIn("C:/Users/TestUser/AppData/Roaming/Codex", roots)
        self.assertIn("C:/Users/TestUser/AppData/Local/OpenAI/Codex", roots)
        self.assertIn("D:/ConfiguredCodex", roots)


if __name__ == "__main__":
    unittest.main()
