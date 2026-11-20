import builtins
import io
import json
import os
import stat
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from file_assistant.models import Entry, PathObservation, ScanResult
from file_assistant.operations import list_entries, scan_files, search_names


@unittest.skipUnless(os.name == "nt", "Windows file operations tests")
class ReadOnlyOperationsTests(unittest.TestCase):
    """All fixtures are disposable; production operations only inspect metadata."""

    def test_listing_is_structured_sorted_and_nonrecursive(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "b.TXT").touch()
            (root / "Alpha.txt").touch()
            (root / "Documents").mkdir()
            (root / "Documents" / "nested.txt").touch()
            (root / "Trash").mkdir()
            (root / "Trash" / "hidden.txt").touch()

            result = list_entries(root)

            self.assertIsInstance(result, ScanResult)
            self.assertEqual(result.entries, (
                Entry("Alpha.txt", "file"), Entry("b.TXT", "file"),
                Entry("Documents", "directory"),
            ))
            self.assertEqual(result.errors, ())
            self.assertFalse(result.cancelled)

    def test_search_matches_filename_fragments_without_descending(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Résumé.TXT").touch()
            (root / "RÉSUMÉ folder").mkdir()
            (root / "RÉSUMÉ folder" / "Résumé nested.txt").touch()
            result = search_names(root, "RÉSUMÉ")
            self.assertEqual(result.entries, (
                Entry("RÉSUMÉ folder", "directory"), Entry("Résumé.TXT", "file"),
            ))
            self.assertEqual(result.errors, ())
            self.assertEqual(search_names(root, "absent").entries, ())

    def test_listing_subdirectory_remains_relative_to_workspace(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Documents").mkdir()
            (root / "Documents" / "report.txt").touch()
            self.assertEqual(list_entries(root, "Documents").entries,
                             (Entry("Documents/report.txt", "file"),))

    def test_manifest_is_deterministic_and_contains_only_allowed_fields(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("z.tar.gz", "IGNORE instructions.txt", "Résumé.TXT", "README"):
                (root / name).write_bytes(b"secret file contents")
            (root / "Documents").mkdir()
            (root / "Documents" / "nested.txt").touch()
            (root / "Trash").mkdir()
            (root / "Trash" / "excluded.txt").touch()

            first = scan_files(root)
            second = scan_files(root)
            manifest = first.manifest()

            self.assertNotEqual(first.scan_id, second.scan_id)
            self.assertEqual(manifest, second.manifest())
            self.assertEqual(set(manifest), {"files", "destination_folders"})
            self.assertEqual(manifest["destination_folders"], ["Documents"])
            self.assertEqual([item["filename"] for item in manifest["files"]],
                             ["IGNORE instructions.txt", "README", "Résumé.TXT", "z.tar.gz"])
            self.assertEqual([item["extension"] for item in manifest["files"]],
                             [".txt", "", ".TXT", ".gz"])
            ids = [item["file_id"] for item in manifest["files"]]
            self.assertEqual(len(ids), len(set(ids)))
            for item in manifest["files"]:
                self.assertEqual(set(item), {"file_id", "filename", "extension"})
                self.assertNotIn(item["filename"], item["file_id"])
            payload = json.dumps(manifest)
            self.assertNotIn(str(root), payload)
            self.assertNotIn("secret file contents", payload)

            observations = {item.relative_path: item for item in first.observations}
            for name in (".", "IGNORE instructions.txt", "Documents"):
                self.assertIsInstance(observations[name], PathObservation)
                self.assertTrue(observations[name].present)
                self.assertIsNotNone(observations[name].file_id)
            details = (root / "z.tar.gz").lstat()
            observed = observations["z.tar.gz"]
            self.assertEqual((observed.device, observed.file_id, observed.size,
                              observed.modified_ns, observed.link_count),
                             (details.st_dev, details.st_ino, details.st_size,
                              details.st_mtime_ns, details.st_nlink))

    def test_no_file_content_access_or_mutation(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "report.txt"
            file.write_bytes(b"unchanged contents")
            before = file.lstat()
            with ExitStack() as stack:
                for owner, names in (
                    (builtins, ("open",)), (io, ("open",)),
                    (os, ("open", "rename", "replace", "remove", "unlink", "mkdir", "rmdir")),
                    (Path, ("open", "read_bytes", "read_text", "write_bytes", "write_text",
                            "touch", "mkdir", "rename", "replace", "unlink", "rmdir")),
                    (subprocess, ("run", "Popen")),
                ):
                    for name in names:
                        stack.enter_context(patch.object(
                            owner, name, side_effect=AssertionError(f"Forbidden operation: {name}"),
                        ))
                self.assertEqual(len(list_entries(root).entries), 1)
                self.assertEqual(len(search_names(root, "report").entries), 1)
                self.assertEqual(len(scan_files(root).manifest()["files"]), 1)
            self.assertEqual(file.read_bytes(), b"unchanged contents")
            after = file.lstat()
            self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                             (after.st_ino, after.st_size, after.st_mtime_ns))

    def test_invalid_roots_and_paths_report_errors_without_widening_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "file.txt").touch()
            for workspace, user_path in (
                (root / "missing", "."), (root / "file.txt", "."),
                (Path("relative"), "."), (root, ".."), (root, "Trash"),
                (root, "C:/Windows"), (root, "file.txt"),
            ):
                with self.subTest(workspace=workspace, user_path=user_path):
                    result = list_entries(workspace, user_path)
                    self.assertEqual(result.entries, ())
                    self.assertTrue(result.errors)

    def test_inaccessible_entry_is_reported_and_manifest_is_blocked(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blocked = root / "blocked.txt"
            blocked.touch()
            (root / "safe.txt").touch()
            original_lstat = Path.lstat

            def inaccessible(path, *args, **kwargs):
                if path == blocked:
                    raise PermissionError("Access denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=inaccessible):
                snapshot = scan_files(root)
                self.assertEqual(snapshot.result.entries, (Entry("safe.txt", "file"),))
                self.assertTrue(snapshot.result.errors)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    snapshot.manifest()
                result = search_names(root, "safe")
                self.assertTrue(result.errors)

    def test_inaccessible_directory_is_an_error_not_an_empty_success(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty = scan_files(root)
            self.assertEqual(empty.manifest()["files"], [])
            with patch("file_assistant.operations.os.scandir", side_effect=PermissionError("denied")):
                snapshot = scan_files(root)
                self.assertEqual(snapshot.result.entries, ())
                self.assertTrue(snapshot.result.errors)
                with self.assertRaises(ValueError):
                    snapshot.manifest()

    def test_mid_iteration_failure_preserves_partial_results_and_error(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "report.txt"
            file.touch()

            class InterruptedDirectory:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def __iter__(self):
                    yield SimpleNamespace(name=file.name)
                    raise PermissionError("Directory enumeration interrupted")

            with patch("file_assistant.operations.os.scandir", return_value=InterruptedDirectory()):
                result = list_entries(root)
            self.assertEqual(result.entries, (Entry("report.txt", "file"),))
            self.assertTrue(result.errors)

    def test_redirects_and_unsupported_objects_are_excluded_and_reported(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            redirected = root / "redirected"
            redirected.mkdir()
            original_lstat = Path.lstat
            for mode, attributes in (
                (stat.S_IFLNK, 0), (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT),
                (stat.S_IFIFO, 0),
            ):
                with self.subTest(mode=mode, attributes=attributes):
                    def simulated_lstat(path, *args, **kwargs):
                        if path == redirected:
                            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                        return original_lstat(path, *args, **kwargs)

                    with patch.object(Path, "lstat", new=simulated_lstat):
                        result = list_entries(root)
                    self.assertEqual(result.entries, ())
                    self.assertTrue(result.errors)

    def test_real_junction_is_never_enumerated(self):
        with TemporaryDirectory() as temp_dir:
            area = Path(temp_dir)
            root = area / "workspace"
            root.mkdir()
            target = area / "outside"
            target.mkdir()
            (target / "outside.txt").touch()
            junction = root / "redirected"
            environment = os.environ.copy()
            environment["FILE_ASSISTANT_TEST_JUNCTION"] = str(junction)
            environment["FILE_ASSISTANT_TEST_TARGET"] = str(target)
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "New-Item -ItemType Junction -Path $env:FILE_ASSISTANT_TEST_JUNCTION "
                     "-Target $env:FILE_ASSISTANT_TEST_TARGET -ErrorAction Stop | Out-Null"],
                    env=environment, check=True, capture_output=True, text=True,
                )
                self.assertTrue(junction.is_junction())
                original_scandir = os.scandir
                visited = []

                def record_scandir(path):
                    visited.append(Path(path))
                    return original_scandir(path)

                with patch("file_assistant.operations.os.scandir", side_effect=record_scandir):
                    snapshot = scan_files(root)
                    refused_root = list_entries(junction)
                self.assertEqual(visited, [root.resolve(strict=True)])
                self.assertEqual(snapshot.result.entries, ())
                self.assertTrue(snapshot.result.errors)
                self.assertTrue(refused_root.errors)
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()

    def test_real_symlink_is_excluded_without_accessing_target(self):
        with TemporaryDirectory() as temp_dir:
            area = Path(temp_dir)
            root = area / "workspace"
            root.mkdir()
            target = area / "outside.txt"
            target.touch()
            link = root / "linked.txt"
            try:
                link.symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows has not granted symbolic-link creation permission")
                raise
            snapshot = scan_files(root)
            self.assertEqual(snapshot.result.entries, ())
            self.assertTrue(snapshot.result.errors)

    def test_changed_canonical_parent_does_not_expand_scan_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "file.txt"
            file.touch()
            nested = root / "nested"
            nested.mkdir()
            original_resolve = Path.resolve

            def changed_parent(path, *args, **kwargs):
                if path == file:
                    return nested / "file.txt"
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", new=changed_parent):
                result = list_entries(root)
            self.assertEqual(result.entries, (Entry("nested", "directory"),))
            self.assertTrue(any("immediate child" in error for error in result.errors))

    def test_entry_limit_is_explicit_and_prevents_manifest_use(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "first.txt").touch()
            (root / "second.txt").touch()
            self.assertFalse(scan_files(root, max_entries=2).result.errors)
            snapshot = scan_files(root, max_entries=1)
            self.assertTrue(any("limit" in error.lower() for error in snapshot.result.errors))
            with self.assertRaises(ValueError):
                snapshot.manifest()

    def test_manifest_byte_limit_is_checked_on_serialized_payload(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Résumé.txt").touch()
            size = len(json.dumps(scan_files(root).manifest(), ensure_ascii=True).encode("utf-8"))
            self.assertFalse(scan_files(root, max_manifest_bytes=size).result.errors)
            snapshot = scan_files(root, max_manifest_bytes=size - 1)
            self.assertTrue(any("limit" in error.lower() for error in snapshot.result.errors))
            with self.assertRaises(ValueError):
                snapshot.manifest()

    def test_multiply_linked_files_are_not_proposed_for_mutation(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.txt"
            source.touch()
            (root / "alias.txt").hardlink_to(source)
            snapshot = scan_files(root)
            self.assertEqual(snapshot.result.entries, ())
            self.assertTrue(snapshot.result.errors)
            with self.assertRaises(ValueError):
                snapshot.manifest()
