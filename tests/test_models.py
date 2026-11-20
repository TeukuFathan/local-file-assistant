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


if __name__ == "__main__":
    unittest.main()
