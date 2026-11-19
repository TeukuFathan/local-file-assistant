"""Qualification result rules, independent of Windows privilege preflight."""

import contextlib
import io
import unittest

from tools import qualify_windows


class RunSuiteTests(unittest.TestCase):
    """Tests for run_suite: qualification must execute passing, unskipped tests."""

    def run_quietly(self, suite):
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                return qualify_windows.run_suite(suite)

    def test_empty_suite_is_rejected(self):
        """No discovered tests must never count as successful qualification."""
        self.assertEqual(self.run_quietly(unittest.TestSuite()), 1)

    def test_passing_suite_is_accepted(self):
        """At least one passing test permits success."""
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        self.assertEqual(self.run_quietly(suite), 0)

    def test_failing_suite_is_rejected(self):
        """An assertion failure prevents qualification."""
        def fail():
            raise AssertionError("intentional fixture failure")
        self.assertEqual(
            self.run_quietly(unittest.TestSuite([unittest.FunctionTestCase(fail)])), 1
        )

    def test_skipped_suite_is_rejected(self):
        """A skipped safety test prevents qualification even without failures."""
        def skip():
            raise unittest.SkipTest("intentional fixture skip")
        self.assertEqual(
            self.run_quietly(unittest.TestSuite([unittest.FunctionTestCase(skip)])), 1
        )
