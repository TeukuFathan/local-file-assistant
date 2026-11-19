"""Run real Windows safety tests; skipped tests prevent qualification."""

import ctypes
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


def run_suite(suite: unittest.TestSuite) -> int:
    """Require actual executed tests, with no failures or skips."""
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        print("Qualification failed: no tests ran.")
        return 1
    if result.skipped:
        print("Qualification failed: skipped tests must be resolved.")
    return 0 if result.wasSuccessful() and not result.skipped else 1


def main() -> int:
    if sys.platform != "win32" or sys.version_info < (3, 12):
        print("Qualification requires Windows and Python 3.12+.")
        return 2

    # This runner requires NTFS for its disposable fixtures; no user files
    # are created or inspected. Preflight failures are infrastructure gaps.
    with TemporaryDirectory() as temp_dir:
        volume = ctypes.create_unicode_buffer(32768)
        filesystem = ctypes.create_unicode_buffer(256)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GetVolumePathNameW(str(temp_dir), volume, len(volume)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.GetVolumeInformationW(
            volume.value, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        print(f"Python: {sys.version.split()[0]}; Windows: {sys.getwindowsversion()}")
        print(f"Test filesystem: {filesystem.value}")
        if filesystem.value != "NTFS":
            print("Qualification requires NTFS temporary storage.")
            return 2
        target = Path(temp_dir) / "target.txt"
        target.touch()
        try:
            (Path(temp_dir) / "link.txt").symlink_to(target)
        except OSError as exc:
            print(f"Real symlink preflight failed: {exc}")
            print("Use a suitably privileged Windows session; do not count skips as passes.")
            return 2

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.chdir(root)
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    return run_suite(suite)


if __name__ == "__main__":
    raise SystemExit(main())
