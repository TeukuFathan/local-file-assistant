import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from file_assistant.workspace import check_sandbox, validate_path


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


@unittest.skipUnless(os.name == "nt", "Windows path validation tests")
class ValidatePathTests(unittest.TestCase):
    """Tests for validate_path: enforce the user-path safety rules."""

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

            # An unexpected exception fails this test. Return-value checks will
            # be added when the function's successful return is implemented.
            validate_path(workspace, "Documents/Trash/report.txt")

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
        """Reject a real junction as the requested folder, an ancestor, or the root."""
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
                )
                for position, root, user_path in cases:
                    with self.subTest(position=position):
                        with self.assertRaisesRegex(ValueError, "Junctions are not allowed"):
                            validate_path(root, user_path)
            finally:
                # Remove only the junction entry, never recursively follow it.
                if os.path.lexists(junction):
                    junction.rmdir()
