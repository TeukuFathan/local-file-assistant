"""T11 lifecycle guarantees; all managed files and app state are disposable."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from file_assistant import recovery
from file_assistant.operations import ExecutionReport, build_organization_plan, scan_files
from file_assistant.ui import (
    ConversationController, Services, SingleWorker, format_report, plan_summary,
)


class ManualWorker:
    """Delay I/O and delivery independently to exercise stale completions."""

    def __init__(self):
        self.jobs = []
        self.results = []

    def submit(self, work, done):
        self.jobs.append((work, done))

    def compute(self):
        work, done = self.jobs.pop(0)
        try:
            value = work()
        except Exception as exc:
            self.results.append((done, None, exc))
        else:
            self.results.append((done, value, None))

    def deliver(self):
        done, value, error = self.results.pop(0)
        done(value, error)

    def run(self):
        self.compute()
        self.deliver()

    def poll(self):
        pass


class FakeServices(Services):
    """Real validation/planning; inert model and executor for controller tests."""

    def __init__(self, state):
        super().__init__(state)
        self.calls = []
        self.reply = {"kind": "proposal", "message": "Review these moves.", "moves": [
            {"file_id": "f1", "destination_folder": "Docs"},
            {"file_id": "f2", "destination_folder": "Pictures"}]}
        self.failure = None
        self.recovery_review = ((), ())

    def inspect(self, root):
        if isinstance(self.recovery_review, Exception):
            raise self.recovery_review
        return self.recovery_review

    def converse(self, manifest, request, *, model, history, diagnostic=None):
        self.calls.append((manifest, request, model, history))
        if self.failure:
            raise self.failure
        return self.reply

    def execute(self, root, plan, **kwargs):
        self.calls.append(("execute", root, plan, kwargs))
        return ExecutionReport(plan.plan_id, "cancelled", (), None,
                               tuple(op.plan_id for op in plan.operations), (), False, "Stopped")


class ControllerFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name)
        self.root = self.area / "managed"
        self.root.mkdir()
        (self.root / "a.txt").write_bytes(b"alpha")
        (self.root / "b.jpg").write_bytes(b"beta")
        self.services = FakeServices(self.area / "state")
        self.worker = ManualWorker()
        self.c = ConversationController(self.services, self.worker)

    def ready(self):
        self.assertTrue(self.c.select_root(self.root))
        self.worker.run()
        self.assertEqual(self.c.state, "ready")
        self.assertTrue(self.c.select_model("fixture:local"))

    def review(self):
        self.ready()
        self.assertTrue(self.c.send("Group by type"))
        self.worker.run()
        self.assertEqual(self.c.state, "reviewable")
        return self.c.plan


class ConversationTests(ControllerFixtures):
    """Only complete current-scan replies can produce actionable reviews."""

    def test_folder_is_required_but_the_requested_default_model_is_already_active(self):
        """Choosing a folder enables requests without a separate model-selection step."""
        self.assertEqual(self.c.model, "qwen3.5:9b")
        self.assertFalse(self.c.send("Organize"))
        self.assertEqual(self.c.draft, "Organize")
        self.assertEqual(self.worker.jobs, [])
        self.c.select_root(self.root)
        self.worker.run()
        self.assertEqual(self.services.calls, [])
        self.assertTrue(self.c.send("Organize"))
        self.worker.run()
        self.assertEqual(self.services.calls[-1][2], "qwen3.5:9b")
        self.assertFalse((self.root / "Docs").exists())

    def test_an_empty_optional_model_override_still_cannot_start_inference(self):
        self.ready()
        self.c.select_model("")
        self.assertFalse(self.c.send("Organize"))
        self.assertEqual(self.worker.jobs, [])
        self.assertEqual(self.services.calls, [])

    def test_invalid_or_state_overlapping_folder_grants_no_root(self):
        """Picker errors never expand scope or authorize an unsafe root."""
        for root in (self.area / "missing", self.area):
            self.c.select_root(root)
            self.worker.run()
            self.assertEqual(self.c.state, "blocked")
            self.assertIsNone(self.c.root)
            self.assertIsNone(self.c.plan)

    def test_clarification_followup_uses_exact_scan_history_and_model(self):
        self.ready()
        question = {"kind": "clarification", "message": "Which grouping?", "moves": []}
        self.services.reply = question
        self.c.send("Organize")
        self.worker.run()
        self.assertEqual(self.c.state, "clarification")
        self.assertIsNone(self.c.plan)
        scan = self.c.snapshot
        self.services.reply = {"kind": "proposal", "message": "Use Docs", "moves": [
            {"file_id": "f1", "destination_folder": "Docs"}]}
        self.c.send("By type")
        self.worker.run()
        self.assertIs(self.c.snapshot, scan)
        self.assertEqual(self.c.plan.scan_id, scan.scan_id)
        manifest, request, model, history = self.services.calls[-1]
        self.assertEqual((manifest, request, model), (scan.manifest(), "By type", "fixture:local"))
        self.assertEqual(history, ({"request": "Organize", "reply": question},))

    def test_followup_invalidates_confirmation_even_on_model_failure(self):
        plan = self.review()
        self.c.open_confirmation(plan.plan_id)
        self.services.failure = OSError("Ollama unavailable")
        self.c.send("Use Finance instead")
        self.assertIsNone(self.c.plan)
        self.assertIsNone(self.c.confirmation)
        self.worker.run()
        self.assertEqual(self.c.state, "blocked")
        self.assertFalse(self.c.confirm_moves(plan.plan_id))
        self.assertEqual(self.c.cards[0].status, "Superseded")

    def test_invalid_and_empty_replies_enable_no_execution(self):
        self.ready()
        for reply in ({"kind": "proposal", "message": "Nothing", "moves": []},
                      {"kind": "clarification", "message": "Bad", "moves": [
                          {"file_id": "f1", "destination_folder": "Docs"}]},
                      {"kind": "proposal", "message": "Bad", "moves": [], "approved": True}):
            self.services.reply = reply
            self.c.send("Organize")
            self.worker.run()
            self.assertIsNone(self.c.plan)
            self.assertFalse(self.c.open_confirmation("old"))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "b.jpg"])

    def test_context_is_bounded_without_truncating_prior_preferences(self):
        self.ready()
        self.services.reply = {"kind": "clarification", "message": "How?", "moves": []}
        for _ in range(8):
            self.assertTrue(self.c.send("Keep original preferences"))
            self.worker.run()
        before = tuple(self.c.history)
        self.assertFalse(self.c.send("Another request"))
        self.assertEqual(tuple(self.c.history), before)
        self.assertFalse(self.c.send("x" * 2001))
        self.assertEqual(len(self.services.calls), 8)

    def test_context_byte_budget_rejects_without_dispatch(self):
        self.ready()
        self.services.reply = {"kind": "clarification", "message": "q" * 4000, "moves": []}
        for _ in range(3):
            self.c.send("x" * 1000)
            self.worker.run()
        before = tuple(self.c.history)
        self.assertFalse(self.c.send("x" * 2000))
        self.assertEqual(tuple(self.c.history), before)

    def test_oversize_completed_exchange_is_rejected_without_losing_history(self):
        """Receiving a reply cannot grow stored context past the same byte bound."""
        self.ready()
        self.services.reply = {"kind": "clarification", "message": "q" * 4000, "moves": []}
        for _ in range(3):
            self.c.send("x" * 1000)
            self.worker.run()
        before = json.loads(json.dumps(self.c.history))
        self.assertTrue(self.c.send("Short follow-up"))
        self.worker.run()
        self.assertEqual(self.c.state, "blocked")
        self.assertEqual(self.c.history, before)
        self.assertIsNone(self.c.plan)
        self.assertEqual(len(before), 3)

    def test_cancel_late_reply_keeps_sole_worker_occupied(self):
        self.ready()
        self.c.send("Organize")
        self.worker.compute()  # A valid old plan exists but has not reached the UI.
        self.c.cancel()
        self.assertTrue(self.c.busy)
        self.assertFalse(self.c.send("Replace"))
        self.worker.deliver()
        self.assertFalse(self.c.busy)
        self.assertIsNone(self.c.plan)
        self.assertEqual(self.c.history, [])

    def test_late_error_after_replacement_does_not_override_new_state(self):
        """Stale failures are discarded just like stale successful proposals."""
        self.ready()
        self.c.send("Old")
        self.services.failure = OSError("Late failure")
        self.worker.compute()
        self.c.rescan()
        self.worker.deliver()
        self.worker.run()
        self.assertEqual(self.c.state, "ready")
        self.assertFalse(any("Late failure" in text for _, text in self.c.messages))

    def test_root_or_scan_change_discards_late_reply_and_old_history(self):
        for action in ("root", "scan"):
            with self.subTest(action=action):
                self.ready()
                self.c.send("Old request")
                self.worker.compute()
                if action == "root":
                    other = self.area / "other"
                    other.mkdir(exist_ok=True)
                    self.c.select_root(other)
                else:
                    self.c.rescan()
                self.worker.deliver()  # old plan cannot reappear before replacement work
                self.assertIsNone(self.c.plan)
                self.assertEqual(self.c.history, [])
                self.worker.run()
                self.assertIsNone(self.c.plan)


class ReviewTests(ControllerFixtures):
    """Review identity and explicit app controls are the approval boundary."""

    def test_subset_rebuild_removes_folders_and_invalidates_old_confirmation(self):
        old = self.review()
        self.c.open_confirmation(old.plan_id)
        self.c.select_moves(old.plan_id, ("f1",))
        self.assertIsNone(self.c.confirmation)
        self.assertIsNone(self.c.plan)  # metadata planning is off the UI thread
        self.worker.run()
        new = self.c.plan
        self.assertNotEqual(new.plan_id, old.plan_id)
        self.assertEqual([op.request.destination for op in new.operations], ["Docs", "Docs/a.txt"])
        self.assertFalse(self.c.confirm_moves(old.plan_id))
        self.assertFalse((self.root / "Docs").exists())

    def test_empty_subset_cannot_open_confirmation(self):
        plan = self.review()
        self.c.select_moves(plan.plan_id, ())
        self.worker.run()
        self.assertEqual(self.c.plan.operations, ())
        self.assertFalse(self.c.open_confirmation(self.c.plan.plan_id))

    def test_root_scan_and_model_changes_invalidate_existing_confirmation(self):
        for action in (lambda: self.c.select_root(self.root), self.c.rescan,
                       lambda: self.c.select_model("other:local")):
            plan = self.review()
            self.c.open_confirmation(plan.plan_id)
            action()
            self.assertIsNone(self.c.confirmation)
            self.assertFalse(self.c.confirm_moves(plan.plan_id))
            while self.worker.jobs:
                self.worker.run()

    def test_execution_exception_retains_uncertainty_and_requires_inspection(self):
        """An unexpected executor failure never leaves a reusable review/context."""
        plan = self.review()
        self.c.open_confirmation(plan.plan_id)
        with patch.object(self.services, "execute", side_effect=OSError("Worker failed")):
            self.c.confirm_moves(plan.plan_id)
            self.worker.run()
        self.assertTrue(self.c.recovery_blocked)
        self.assertIsNone(self.c.plan)
        self.assertIsNone(self.c.snapshot)
        self.assertEqual(self.c.history, [])
        self.assertFalse(self.c.send("Retry"))

    def test_only_confirm_current_summary_approves_once_and_freezes_context(self):
        plan = self.review()
        self.assertFalse(self.c.confirm_moves(plan.plan_id))
        self.assertTrue(self.c.open_confirmation(plan.plan_id))
        self.assertIs(self.c.confirmation, plan)
        self.assertFalse((self.root / "Docs").exists())
        with patch.object(self.services, "approve", wraps=self.services.approve) as approve:
            self.assertTrue(self.c.confirm_moves(plan.plan_id))
            self.assertFalse(self.c.confirm_moves(plan.plan_id))
            self.assertFalse(self.c.select_root(self.area))
            self.assertFalse(self.c.rescan())
            self.assertFalse(self.c.select_model("other"))
            self.assertFalse(self.c.send("Change it"))
            approve.assert_called_once_with(plan, workspace=self.root.resolve(),
                                            scan_id=plan.scan_id, confirmed=True)
        self.c.cancel()
        self.worker.run()
        _, root, executed, args = self.services.calls[-1]
        self.assertIs(executed, plan)
        self.assertEqual((root, args["scan_id"]), (self.root.resolve(), plan.scan_id))
        self.assertTrue(args["cancel_event"].is_set())
        self.assertEqual(self.c.state, "finished")
        self.assertEqual(self.c.report.not_started, tuple(op.plan_id for op in plan.operations))
        self.assertIsNone(self.c.snapshot)
        self.assertEqual(self.c.history, [])

    def test_cancel_confirmation_and_typed_approval_never_execute(self):
        plan = self.review()
        self.c.open_confirmation(plan.plan_id)
        self.c.cancel_confirmation()
        self.assertFalse(self.c.confirm_moves(plan.plan_id))
        for text in ("yes", "apply", "go ahead"):
            self.c.send(text)
            if self.worker.jobs:
                self.worker.run()
        self.assertFalse(any(call[0] == "execute" for call in self.services.calls))
        self.assertFalse((self.root / "Docs").exists())


class RecoveryAndCloseTests(ControllerFixtures):
    """Recovery needs a separate exact review; close never joins the worker."""

    def test_corrupt_recovery_stays_blocked_without_acknowledgement(self):
        self.services.recovery_review = recovery.RecoveryRequired("Corrupt journal; preserve it")
        self.c.select_root(self.root)
        self.worker.run()
        self.assertEqual(self.c.state, "blocked")
        self.assertFalse(self.c.send("yes"))
        self.assertFalse(self.c.open_recovery_confirmation())
        self.assertFalse(self.c.confirm_recovery())

    def test_separate_recovery_confirmation_passes_captured_evidence(self):
        journal, finding = object(), object()
        captured = ((journal,), (finding,))
        self.services.recovery_review = captured
        self.c.select_root(self.root)
        self.worker.run()
        self.assertEqual(self.c.state, "blocked")
        self.assertFalse(self.c.confirm_recovery())
        self.assertTrue(self.c.open_recovery_confirmation())
        with patch.object(self.services, "acknowledge") as acknowledge:
            self.assertTrue(self.c.confirm_recovery(captured))
            self.assertFalse(self.c.confirm_recovery(captured))
            self.worker.run()
            acknowledge.assert_called_once_with(self.root.resolve(), captured)
        self.assertIsNone(self.c.plan)

    def test_replaced_recovery_confirmation_cannot_acknowledge_new_findings(self):
        self.services.recovery_review = ((object(),), (object(),))
        self.c.select_root(self.root)
        self.worker.run()
        self.c.open_recovery_confirmation()
        old = self.c.recovery_confirmation
        self.services.recovery_review = ((object(),), (object(),))
        self.c.rescan()
        self.worker.run()
        self.c.open_recovery_confirmation()
        self.assertFalse(self.c.confirm_recovery(old))

    def test_failed_reinspection_blocks_chat_and_preserves_old_journals(self):
        self.ready()
        self.services.recovery_review = recovery.RecoveryRequired("Corrupt journal")
        self.c.rescan()
        self.worker.run()
        self.assertTrue(self.c.recovery_blocked)
        self.assertFalse(self.c.send("Ignore recovery"))

    def test_close_during_execution_waits_for_actual_outcome(self):
        plan = self.review()
        self.c.open_confirmation(plan.plan_id)
        self.c.confirm_moves(plan.plan_id)
        self.c.close()
        self.assertFalse(self.c.closed)
        self.assertTrue(self.c.cancel_event.is_set())
        self.worker.run()
        self.assertTrue(self.c.closed)
        self.assertEqual(self.c.report.status, "cancelled")

    def test_close_during_inference_discards_late_cards(self):
        self.ready()
        self.c.send("Organize")
        self.worker.compute()
        self.c.close()
        self.assertFalse(self.c.closed)
        self.worker.deliver()
        self.assertTrue(self.c.closed)
        self.assertIsNone(self.c.plan)


class WorkerTests(unittest.TestCase):
    def test_work_runs_off_thread_and_completion_only_on_poll(self):
        """Tk-facing callbacks execute only on the caller's polling thread."""
        worker = SingleWorker()
        main = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        seen = []

        def work():
            started.set()
            release.wait(2)
            return threading.get_ident()

        worker.submit(work, lambda value, error: seen.append((value, error, threading.get_ident())))
        self.assertTrue(started.wait(2))
        with self.assertRaises(RuntimeError):
            worker.submit(work, lambda *args: None)
        release.set()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            worker.poll()
            time.sleep(.005)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0][0], main)
        self.assertEqual(seen[0][1:], (None, main))


class ServiceIntegrationTests(ControllerFixtures):
    def test_real_scan_transport_plan_confirmation_execution_preserves_files(self):
        """Only explicit confirmation mutates disposable files; no real model is used."""
        self.c = ConversationController(Services(self.area / "state"), self.worker)
        self.ready()
        payloads = []

        def transport(host, port, path, payload, timeout, *, diagnostic=None):
            payloads.append((path, json.loads(payload)))
            if path == "/api/show":
                return {"details": {"format": "gguf"}, "model_info": {"architecture": "fixture"}}
            return {"done": True, "done_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(self.services.reply)}}

        with patch("file_assistant.ollama_client._post_json", side_effect=transport):
            self.c.send("Group by type")
            self.worker.run()
        plan = self.c.plan
        self.assertIsNotNone(plan)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "b.jpg"])
        data = json.loads(payloads[-1][1]["messages"][1]["content"])
        self.assertEqual(set(data), {"manifest", "request", "history"})
        self.assertNotIn(str(self.root), json.dumps(data))
        self.c.open_confirmation(plan.plan_id)
        self.assertFalse((self.root / "Docs").exists())
        self.c.confirm_moves(plan.plan_id)
        self.worker.run()
        self.assertEqual(self.c.report.status, "success")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / "Pictures/b.jpg").read_bytes(), b"beta")
        self.assertEqual(set(self.c.report.created_folders), {"Docs", "Pictures"})
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            self.assertTrue(all(recovery.is_complete(j) for j in recovery.load_journals(lock)))

    def test_real_recovery_requires_exact_acknowledgement_and_keeps_journal(self):
        """Interrupted disposable batches are inspected, retained, never resumed."""
        snapshot = scan_files(self.root)
        plan = build_organization_plan(self.root, snapshot, {"moves": self.services.reply["moves"]},
                                       proposal_scan_id=snapshot.scan_id)
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            recovery.begin_batch(lock, plan)
        self.c = ConversationController(Services(self.area / "state"), self.worker)
        self.c.select_root(self.root)
        self.worker.run()
        captured = self.c.recovery_review
        self.assertEqual(self.c.state, "blocked")
        self.assertFalse(self.c.confirm_recovery(captured))
        self.c.open_recovery_confirmation()
        self.c.confirm_recovery(captured)
        self.worker.run()
        self.assertEqual(self.c.state, "ready")
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            journals = recovery.load_journals(lock)
        self.assertEqual(len(journals), 1)
        self.assertIsNotNone(journals[0].resolution)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "b.jpg"])

    def test_real_executor_cancellation_reports_partial_effects_without_rollback(self):
        """UI passes its Event to T9 and displays completed versus untouched work."""
        self.c = ConversationController(Services(self.area / "state"), self.worker)
        self.ready()
        with patch.object(self.c.services, "converse", return_value=self.services.reply):
            self.c.send("Group by type")
            self.worker.run()
        plan = self.c.plan
        self.c.open_confirmation(plan.plan_id)
        self.c.confirm_moves(plan.plan_id)
        from file_assistant import operations
        move = operations._move_no_replace

        def first_move_then_stop(source, destination):
            move(source, destination)
            self.c.cancel_event.set()

        with patch.object(operations, "_move_no_replace", side_effect=first_move_then_stop):
            self.worker.run()
        self.assertEqual(self.c.report.status, "cancelled")
        self.assertEqual(len(self.c.report.completed), 3)  # two mkdirs plus one move
        self.assertEqual(self.c.report.not_started, (plan.operations[-1].plan_id,))
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / "b.jpg").read_bytes(), b"beta")
        self.assertTrue((self.root / "Pictures").is_dir())
        self.assertIsNone(self.c.snapshot)

    def test_real_recovery_changed_findings_refuse_acknowledgement(self):
        """The service must pass the displayed evidence rather than reapprove fresh findings."""
        snapshot = scan_files(self.root)
        plan = build_organization_plan(self.root, snapshot, {"moves": self.services.reply["moves"]},
                                       proposal_scan_id=snapshot.scan_id)
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            recovery.begin_batch(lock, plan)
            recovery.record_intent(lock, plan.plan_id, plan.operations[0].plan_id)
        services = Services(self.area / "state")
        review = services.inspect(self.root)
        (self.root / "Docs").mkdir()
        with self.assertRaises(recovery.RecoveryRequired):
            services.acknowledge(self.root, review)
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            self.assertIsNone(recovery.load_journals(lock)[0].resolution)


class PresentationTests(ControllerFixtures):
    def test_summary_and_partial_report_use_exact_plan_and_python_results(self):
        """Long paths stay inspectable; partial effects are never inferred from prose."""
        plan = self.review()
        summary = plan_summary(plan)
        self.assertIn(str(plan.root), summary)
        self.assertIn("a.txt  ->  Docs/a.txt", summary)
        self.assertIn("New folders: Docs, Pictures", summary)
        report = ExecutionReport(plan.plan_id, "cancelled", (), None,
                                 tuple(op.plan_id for op in plan.operations), ("Docs",), False, "Stopped")
        text = format_report(report, plan)
        self.assertIn("Not started: a.txt -> Docs/a.txt", text)
        self.assertIn("Created folders: Docs", text)


if __name__ == "__main__":
    unittest.main()
