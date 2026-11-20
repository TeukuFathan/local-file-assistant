import ctypes
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from file_assistant.workspace import (
    check_sandbox, validate_destination, validate_path, validate_workspace,
)


class CheckSandboxTests(unittest.TestCase):
    """Tests for check_sandbox: identify existing directories."""

    def test_existing_directory_returns_true(self):
        """An existing directory is accepted."""
        with TemporaryDirectory() as temp_dir:
            directory_path = Path(temp_dir)

            self.assertTrue(check_sandbox(directory_path))

    def test_missing_path_returns_false(self):
        """A path that does not exist is refused."""
        with TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing"

            self.assertFalse(check_sandbox(missing_path))

    def test_file_returns_false(self):
        """An existing regular file is not accepted as a directory."""
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.txt"
            file_path.touch()

            self.assertFalse(check_sandbox(file_path))


class ValidateWorkspaceTests(unittest.TestCase):
    """Tests for the configured root before a user path is inspected."""

    def test_existing_absolute_directory_is_accepted(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            self.assertEqual(validate_workspace(workspace), workspace.resolve(strict=True))

    def test_root_with_dot_dot_returns_canonical_directory(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "child").mkdir()

            self.assertEqual(
                validate_workspace(workspace / "child" / ".."),
                workspace.resolve(strict=True),
            )

    def test_relative_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Workspace must be absolute"):
            validate_workspace(Path("relative-workspace"))

    @unittest.skipUnless(os.name == "nt", "Windows root namespace test")
    def test_unc_and_device_namespace_roots_are_rejected(self):
        for root in (r"\\server\share\folder", r"\\?\C:\folder", r"\\.\C:\folder"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "local drive"):
                    validate_workspace(Path(root))

    @unittest.skipUnless(os.name == "nt", "Windows root name test")
    def test_root_with_trailing_dot_component_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "child").mkdir()
            with self.assertRaisesRegex(ValueError, "Invalid Windows path component"):
                validate_workspace(workspace / "child.")

    def test_missing_root_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                validate_workspace(Path(temp_dir) / "missing")

    def test_file_root_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            file_root = Path(temp_dir) / "file.txt"
            file_root.touch()

            with self.assertRaises(NotADirectoryError):
                validate_workspace(file_root)

    def test_inaccessible_root_reports_permission_error(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            original_lstat = Path.lstat

            def inaccessible_root(path, *args, **kwargs):
                if path == workspace:
                    raise PermissionError("Access denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=inaccessible_root):
                with self.assertRaises(PermissionError):
                    validate_workspace(workspace)

    @unittest.skipUnless(os.name == "nt", "Windows reparse-point test")
    def test_unsupported_reparse_point_in_root_or_ancestor_is_rejected(self):
        """Unknown reparse types must not be followed through the configured root."""
        with TemporaryDirectory() as temp_dir:
            ancestor = Path(temp_dir) / "ancestor"
            workspace = ancestor / "workspace"
            workspace.mkdir(parents=True)
            original_lstat = Path.lstat

            for reparse_path in (ancestor, workspace):
                with self.subTest(reparse_path=reparse_path):
                    def simulated_lstat(path, *args, **kwargs):
                        result = original_lstat(path, *args, **kwargs)
                        if path == reparse_path:
                            return type("ReparseStat", (), {
                                "st_file_attributes": result.st_file_attributes
                                | stat.FILE_ATTRIBUTE_REPARSE_POINT,
                                "st_mode": result.st_mode,
                            })()
                        return result

                    with patch.object(Path, "is_symlink", return_value=False), \
                            patch.object(Path, "is_junction", return_value=False), \
                            patch.object(Path, "lstat", new=simulated_lstat):
                        with self.assertRaisesRegex(ValueError, "Reparse points are not allowed"):
                            validate_workspace(workspace)


@unittest.skipUnless(os.name == "nt", "Windows path validation tests")
class ValidatePathTests(unittest.TestCase):
    """Tests for validate_path: enforce the user-path safety rules."""

    def test_file_workspace_is_rejected(self):
        """An existing user name cannot make a file root a valid workspace."""
        with TemporaryDirectory() as temp_dir:
            file_root = Path(temp_dir) / "file.txt"
            file_root.touch()

            with self.assertRaises(NotADirectoryError):
                validate_path(file_root, "report.txt")

    # Rule: user paths must be relative to the configured workspace.
    def test_absolute_paths_are_rejected(self):
        """Reject drive-absolute and network paths, with either slash style."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for user_path in ("C:/Windows", r"C:\Windows", r"\\server\share\file.txt"):
                with self.subTest(user_path=user_path):
                    with self.assertRaisesRegex(ValueError, r"^Path must be relative\.$"):
                        validate_path(workspace, user_path)

    def test_drive_relative_path_is_rejected(self):
        """Reject C:Documents, which depends on a drive's current directory."""
        with TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, r"^Path must be relative\.$"):
                validate_path(Path(temp_dir), "C:Documents")

    def test_rooted_paths_are_rejected(self):
        """Reject paths starting at a drive root even without a drive letter."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for user_path in ("/Documents", r"\Documents"):
                with self.subTest(user_path=user_path):
                    with self.assertRaisesRegex(ValueError, r"^Path must be relative\.$"):
                        validate_path(workspace, user_path)

    def test_device_namespace_paths_are_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for user_path in (r"\\?\C:\Windows", r"\\.\C:\Windows"):
                with self.subTest(user_path=user_path):
                    with self.assertRaisesRegex(ValueError, "Path must be relative"):
                        validate_path(workspace, user_path)

    # Rule: no parent-directory traversal, wherever it appears in a path.
    def test_parent_traversal_is_rejected(self):
        """Reject .. on its own and within paths using either slash style."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for user_path in ("..", "../Private", "Documents/../Private", r"Documents\..\Private"):
                with self.subTest(user_path=user_path):
                    with self.assertRaisesRegex(ValueError, r"^Parent traversal is not allowed\.$"):
                        validate_path(workspace, user_path)

    # Rule: only the workspace's top-level Trash folder is reserved.
    def test_top_level_trash_is_rejected(self):
        """Reject Trash itself and its children regardless of capitalization."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for user_path in ("Trash", "TRASH/report.txt", "trash/report.txt"):
                with self.subTest(user_path=user_path):
                    with self.assertRaisesRegex(ValueError, "Trash is reserved"):
                        validate_path(workspace, user_path)

    def test_nested_trash_name_is_not_reserved(self):
        """A normal subfolder named Trash must not trigger the reserved-folder rule."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            nested_folder = workspace / "Documents" / "Trash"
            nested_folder.mkdir(parents=True)
            (nested_folder / "report.txt").touch()

            self.assertEqual(
                validate_path(workspace, "Documents/Trash/report.txt"),
                (nested_folder / "report.txt").resolve(strict=True),
            )

    def test_dot_selects_root_but_empty_input_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            self.assertEqual(validate_path(workspace, "."), workspace.resolve(strict=True))
            with self.assertRaisesRegex(ValueError, "empty"):
                validate_path(workspace, "")

    def test_regular_file_returns_canonical_path(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            file = workspace / "Résumé.txt"
            file.touch()
            self.assertEqual(validate_path(workspace, "Résumé.txt"), file.resolve(strict=True))

    def test_case_variant_of_existing_file_returns_canonical_path(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            file = workspace / "MixedCase.txt"
            file.touch()
            if not (workspace / "mixedcase.TXT").exists():
                self.skipTest("Test filesystem is case-sensitive")
            self.assertEqual(validate_path(workspace, "mixedcase.TXT"), file.resolve(strict=True))

    def test_existing_short_name_alias_resolves_to_long_name_when_enabled(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            folder = workspace / "LongDirectoryNameForAlias"
            folder.mkdir()
            get_short_path = ctypes.windll.kernel32.GetShortPathNameW
            get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_short_path.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            size = get_short_path(str(folder), buffer, len(buffer))
            if not size or size >= len(buffer):
                self.skipTest("Short-name lookup is unavailable")
            short_name = Path(buffer.value).name
            if short_name.casefold() == folder.name.casefold():
                self.skipTest("8.3 aliases are disabled on this volume")

            self.assertEqual(validate_path(workspace, short_name), folder.resolve(strict=True))

    def test_invalid_windows_components_are_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for name in ("CON", "con.txt", "NUL.tar.gz", "LPT9.log", "COM¹",
                         "lpt³.txt", "file:stream", "a<bad", "a>bad", 'a"bad',
                         "a|bad", "a?bad", "a*bad", "a\x01bad", "trailing.",
                         "trailing ", "folder./file.txt", "folder /file.txt"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "Invalid Windows path component"):
                        validate_path(workspace, name)

    def test_long_existing_path_returns_canonical_path(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            parent = workspace
            try:
                for index in range(4):
                    parent = parent / (f"section{index}_" + "x" * 55)
                    parent.mkdir()
                file = parent / "report.txt"
                file.touch()
            except OSError as exc:
                self.skipTest(f"Long paths are not enabled for this test environment: {exc}")

            self.assertGreater(len(str(file)), 260)
            self.assertEqual(
                validate_path(workspace, str(file.relative_to(workspace))),
                file.resolve(strict=True),
            )

    def test_unsupported_reparse_point_in_requested_path_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            item = workspace / "item.txt"
            item.touch()
            original_lstat = Path.lstat

            def simulated_lstat(path, *args, **kwargs):
                result = original_lstat(path, *args, **kwargs)
                if path == item:
                    return type("ReparseStat", (), {
                        "st_file_attributes": getattr(result, "st_file_attributes", 0)
                        | stat.FILE_ATTRIBUTE_REPARSE_POINT,
                        "st_mode": result.st_mode,
                    })()
                return result

            with patch.object(Path, "is_symlink", return_value=False), \
                    patch.object(Path, "is_junction", return_value=False), \
                    patch.object(Path, "lstat", new=simulated_lstat):
                with self.assertRaisesRegex(ValueError, "Reparse points are not allowed"):
                    validate_path(workspace, "item.txt")

    def test_resolved_sibling_prefix_is_outside_workspace(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "work"
            workspace.mkdir()
            item = workspace / "item.txt"
            item.touch()
            sibling = Path(temp_dir) / "workspace" / "item.txt"
            original_resolve = Path.resolve

            def simulated_resolve(path, *args, **kwargs):
                if path == item:
                    return sibling
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", new=simulated_resolve):
                with self.assertRaisesRegex(ValueError, "outside the workspace"):
                    validate_path(workspace, "item.txt")

    def test_canonical_trash_alias_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            alias = workspace / "TRASH~1"
            alias.mkdir()
            trash = workspace / "Trash"
            original_resolve = Path.resolve

            def simulated_resolve(path, *args, **kwargs):
                if path == alias:
                    return trash
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", new=simulated_resolve):
                with self.assertRaisesRegex(ValueError, "Trash is reserved"):
                    validate_path(workspace, "TRASH~1")

    def test_inaccessible_requested_path_reports_permission_error(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            item = workspace / "item.txt"
            item.touch()
            original_lstat = Path.lstat

            def inaccessible(path, *args, **kwargs):
                if path == item:
                    raise PermissionError("Access denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=inaccessible):
                with self.assertRaises(PermissionError):
                    validate_path(workspace, "item.txt")

    # Rule: this validator initially supports existing paths only.
    def test_missing_file_is_rejected(self):
        """A missing file inside an existing workspace raises FileNotFoundError."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            # Do not create missing.txt: its absence is the scenario under test.
            with self.assertRaises(FileNotFoundError):
                validate_path(workspace, "missing.txt")

    # Rule: symbolic links are refused, even when their targets exist.
    def test_detected_symlink_is_rejected(self):
        """Verify rejection logic by simulating link detection without Windows privileges."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "linked.txt").touch()
            # This tests the response to link detection, not real OS detection.
            # The integration test below checks an actual link when permitted.
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                    validate_path(workspace, "linked.txt")

    def test_detected_parent_symlink_is_rejected(self):
        """Reject a link in an ancestor folder even when the final file is not a link."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            linked_folder = workspace / "linked_folder"
            nested_folder = linked_folder / "nested"
            nested_folder.mkdir(parents=True)
            (nested_folder / "report.txt").touch()

            # Simulate only linked_folder being a symlink. The final file and
            # its immediate parent are ordinary paths, so checking only those
            # must not be enough. This does not test real Windows link detection.
            def is_simulated_link(path):
                return path == linked_folder

            with patch.object(Path, "is_symlink", new=is_simulated_link):
                with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                    validate_path(workspace, "linked_folder/nested/report.txt")

    def test_detected_workspace_symlink_is_rejected(self):
        """Reject a linked workspace itself, not just links beneath it."""
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "report.txt").touch()

            # Only the workspace is simulated as a link; its child is not.
            def is_simulated_link(path):
                return path == workspace

            with patch.object(Path, "is_symlink", new=is_simulated_link):
                with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                    validate_path(workspace, "report.txt")

    def test_detected_workspace_ancestor_symlink_is_rejected(self):
        """A redirect above the root must be rejected before the user path is used."""
        with TemporaryDirectory() as temp_dir:
            ancestor = Path(temp_dir) / "ancestor"
            workspace = ancestor / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "report.txt").touch()

            with patch.object(Path, "is_symlink", new=lambda path: path == ancestor):
                with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                    validate_path(workspace, "report.txt")

    def test_file_symlink_outside_workspace_is_rejected(self):
        """Reject a file link that redirects from the workspace to outside it."""
        with TemporaryDirectory() as temp_dir:
            test_root = Path(temp_dir)
            workspace = test_root / "workspace"
            workspace.mkdir()
            # Both the outside target and link are disposable test data.
            target = test_root / "outside.txt"
            target.touch()
            link = workspace / "linked.txt"
            try:
                link.symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows has not granted symbolic-link creation permission")
                raise

            with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                validate_path(workspace, "linked.txt")

    # Rule: Windows directory junctions are refused, just like symbolic links.
    def test_directory_junction_is_rejected(self):
        """Reject a real junction as a requested folder, root, or ancestor."""
        with TemporaryDirectory() as temp_dir:
            test_root = Path(temp_dir)
            workspace = test_root / "workspace"
            workspace.mkdir()
            target = test_root / "outside"
            target.mkdir()
            nested = target / "nested"
            nested.mkdir()
            (nested / "report.txt").touch()
            junction = workspace / "redirected"

            # Paths are passed as environment data, not inserted into shell code.
            # Both ends of the junction stay inside this disposable test area.
            environment = os.environ.copy()
            environment["FILE_ASSISTANT_TEST_JUNCTION"] = str(junction)
            environment["FILE_ASSISTANT_TEST_TARGET"] = str(target)
            try:
                subprocess.run(
                    [
                        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "New-Item -ItemType Junction "
                        "-Path $env:FILE_ASSISTANT_TEST_JUNCTION "
                        "-Target $env:FILE_ASSISTANT_TEST_TARGET "
                        "-ErrorAction Stop | Out-Null",
                    ],
                    env=environment, check=True, capture_output=True, text=True,
                )
                # Confirm the fixture really is a junction before testing rejection.
                self.assertEqual(
                    junction.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT
                )
                # Check all three positions separately so one failure does not
                # hide whether workspace-root protection already works.
                cases = (
                    ("requested folder", workspace, "redirected"),
                    ("ancestor folder", workspace, "redirected/nested/report.txt"),
                    ("workspace root", junction, "nested/report.txt"),
                    ("workspace ancestor", junction / "nested", "report.txt"),
                )
                for position, root, user_path in cases:
                    with self.subTest(position=position):
                        with self.assertRaisesRegex(ValueError, "Junctions are not allowed"):
                            validate_path(root, user_path)
            finally:
                # Remove only the junction entry, never recursively follow it.
                if os.path.lexists(junction):
                    junction.rmdir()


@unittest.skipUnless(os.name == "nt", "Windows destination validation tests")
class ValidateDestinationTests(unittest.TestCase):
    """Missing parents require one explicitly named new root child."""

    def test_planned_folder_returns_file_path_without_creating_anything(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = validate_destination(
                workspace, "Documents/Résumé.txt", planned_folder="Documents",
            )

            self.assertEqual(result, workspace.resolve(strict=True) / "Documents" / "Résumé.txt")
            self.assertEqual(list(workspace.iterdir()), [])
            # The existing-path API must still require every component to exist.
            for name in ("Documents", "Documents/Résumé.txt"):
                with self.subTest(name=name):
                    with self.assertRaises(FileNotFoundError):
                        validate_path(workspace, name)

    def test_missing_parent_requires_explicit_matching_folder(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            with self.assertRaises(FileNotFoundError):
                validate_destination(workspace, "Documents/report.txt")
            with self.assertRaises(ValueError):
                validate_destination(workspace, "Other/report.txt", planned_folder="Documents")

    def test_planned_folder_cannot_allow_additional_missing_ancestors(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for destination, folder in (
                ("Documents/nested/report.txt", "Documents"),
                ("Documents/nested/report.txt", "Documents/nested"),
                ("missing/Documents/report.txt", "Documents"),
                ("report.txt", "Documents"),
            ):
                with self.subTest(destination=destination, folder=folder):
                    with self.assertRaises(ValueError):
                        validate_destination(workspace, destination, planned_folder=folder)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_unsafe_planned_folder_names_are_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for folder in ("", ".", "..", "Trash", "TRASH", "CON", "file:stream",
                           "bad?", "trailing.", "trailing ", "C:folder", "C:/folder",
                           "/folder", r"\folder", r"\\server\share", "Documents/."):
                with self.subTest(folder=folder):
                    with self.assertRaises(ValueError):
                        validate_destination(
                            workspace, f"{folder}/report.txt", planned_folder=folder,
                        )

    def test_invalid_file_basename_in_planned_folder_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            for basename in ("", ".", "..", "NUL.txt", "file:stream", "bad?", "x\x00"):
                with self.subTest(basename=basename):
                    with self.assertRaises(ValueError):
                        validate_destination(
                            workspace, f"Documents/{basename}", planned_folder="Documents",
                        )

    def test_occupied_planned_folder_name_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "Directory").mkdir()
            (workspace / "file.txt").touch()
            for folder in ("Directory", "directory", "file.txt"):
                with self.subTest(folder=folder):
                    with self.assertRaises(FileExistsError):
                        validate_destination(
                            workspace, f"{folder}/report.txt", planned_folder=folder,
                        )

    def test_case_collision_is_rejected_even_if_direct_lookup_reports_absent(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "Documents").mkdir()
            original_lstat = Path.lstat

            def case_sensitive_lookup(path, *args, **kwargs):
                if path.name == "documents":
                    raise FileNotFoundError(path)
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=case_sensitive_lookup):
                with self.assertRaises(FileExistsError):
                    validate_destination(
                        workspace, "documents/report.txt", planned_folder="documents",
                    )

    def test_planned_folder_is_joined_to_canonical_root(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "existing").mkdir()
            root_spelling = workspace / "existing" / ".."
            self.assertEqual(
                validate_destination(
                    root_spelling, "Documents/report.txt", planned_folder="Documents",
                ),
                workspace.resolve(strict=True) / "Documents" / "report.txt",
            )
            self.assertFalse((workspace / "Documents").exists())

    def test_existing_folder_and_new_folder_destinations_still_work(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            parent = workspace / "Documents"
            parent.mkdir()
            self.assertEqual(
                validate_destination(workspace, "Documents/report.txt"),
                parent.resolve(strict=True) / "report.txt",
            )
            self.assertEqual(
                validate_destination(workspace, "NewFolder"),
                workspace.resolve(strict=True) / "NewFolder",
            )
            self.assertFalse((workspace / "NewFolder").exists())

    def test_occupied_destination_and_file_parent_are_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "report.txt").touch()
            with self.assertRaises(FileExistsError):
                validate_destination(workspace, "REPORT.TXT")
            with self.assertRaises(NotADirectoryError):
                validate_destination(workspace, "report.txt/child.txt")

    def test_planned_folder_revalidates_root_and_ancestors(self):
        with TemporaryDirectory() as temp_dir:
            ancestor = Path(temp_dir)
            workspace = ancestor / "workspace"
            workspace.mkdir()
            for redirected in (ancestor, workspace):
                with self.subTest(redirected=redirected):
                    with patch.object(Path, "is_symlink", new=lambda path: path == redirected):
                        with self.assertRaisesRegex(ValueError, "Symbolic links are not allowed"):
                            validate_destination(
                                workspace, "Documents/report.txt", planned_folder="Documents",
                            )
            with self.assertRaises(ValueError):
                validate_destination(Path("relative"), "Documents/report.txt", planned_folder="Documents")

    def test_redirected_existing_parent_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            parent = workspace / "Documents"
            parent.mkdir()
            with patch.object(Path, "is_junction", new=lambda path: path == parent):
                with self.assertRaisesRegex(ValueError, "Junctions are not allowed"):
                    validate_destination(workspace, "Documents/report.txt")

    def test_canonical_existing_parent_cannot_escape_or_alias_trash(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "work"
            workspace.mkdir()
            parent = workspace / "alias"
            parent.mkdir()
            original_resolve = Path.resolve
            for resolved in (Path(temp_dir) / "workspace", workspace / "Trash"):
                with self.subTest(resolved=resolved):
                    def simulated_resolve(path, *args, **kwargs):
                        if path == parent:
                            return resolved
                        return original_resolve(path, *args, **kwargs)

                    with patch.object(Path, "resolve", new=simulated_resolve):
                        with self.assertRaises(ValueError):
                            validate_destination(workspace, "alias/report.txt")

    def test_inaccessible_planned_folder_is_not_treated_as_absent(self):
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            folder = workspace / "Documents"
            original_lstat = Path.lstat

            def inaccessible(path, *args, **kwargs):
                if path == folder:
                    raise PermissionError("Access denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=inaccessible):
                with self.assertRaises(PermissionError):
                    validate_destination(workspace, "Documents/report.txt", planned_folder="Documents")
