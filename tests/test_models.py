"""Tests for the immutable shared model contracts."""

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from file_assistant.models import (
    Entry, OperationPlan, OperationRequest, OperationResult, PathObservation,
    ScanResult,
)


class ModelTests(unittest.TestCase):
    """Validate model invariants and immutability."""

    def test_entry_and_scan_result_are_immutable(self):
        entry = Entry("docs/readme.txt", "file")
        result = ScanResult((entry,), (), False)
        with self.assertRaises(FrozenInstanceError):
            entry.kind = "directory"
        self.assertEqual(result.entries, (entry,))

    def test_entry_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            Entry("x", "link")

    def test_request_enforces_action_fields(self):
        self.assertEqual(OperationRequest("mkdir", destination="new" ).action, "mkdir")
        self.assertEqual(OperationRequest("move", "a", "b").source, "a")
        self.assertEqual(OperationRequest("trash", "a").action, "trash")
        self.assertEqual(OperationRequest("restore", trash_id="id").trash_id, "id")
        for request in (
            lambda: OperationRequest("mkdir"),
            lambda: OperationRequest("trash", "a", "b"),
            lambda: OperationRequest("restore"),
            lambda: OperationRequest("move", "C:/a", "b"),
            lambda: OperationRequest("bad"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                request()

    def test_plan_and_result_accept_frozen_value_contracts(self):
        request = OperationRequest("trash", "report.txt")
        observation = PathObservation("report.txt", True, device=1, file_id=2)
        plan = OperationPlan("p1", Path("C:/workspace"), (1, 2), request, (observation,))
        result = OperationResult(plan.plan_id, "success", "Moved to Trash", "t1")
        with self.assertRaises(FrozenInstanceError):
            plan.plan_id = "changed"
        self.assertEqual(result.trash_id, "t1")


    # OperationRequest: strict shape checks precede filesystem authorization.
    def test_request_rejects_rooted_paths_in_either_field(self):
        """Reject Windows roots without drive letters in sources/destinations."""
        for name in ("/Windows", chr(92) + "Windows"):
            for source, destination in ((name, "safe"), ("safe", name)):
                with self.subTest(source=source, destination=destination):
                    with self.assertRaises(ValueError):
                        OperationRequest("move", source, destination)

    def test_request_forbidden_fields_must_be_none(self):
        """Empty strings and other false values are not absent fields."""
        for value in ("", False, 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OperationRequest("mkdir", source=value, destination="new")
                with self.assertRaises(ValueError):
                    OperationRequest("trash", source="file", trash_id=value)

    def test_request_required_fields_must_be_nonempty_strings(self):
        """Reject malformed tool argument types instead of trusting type hints."""
        for value in (None, "", True, 1, ["name"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OperationRequest("restore", trash_id=value)


if __name__ == "__main__":
    unittest.main()
