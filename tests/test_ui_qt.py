"""Real offscreen Qt controls preserve T11's app-owned authorization boundary."""

import json
import os
import threading
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QPlainTextEdit, QPushButton

from file_assistant import recovery
from file_assistant.ui import ConversationController, DesktopApp, Services, SingleWorker
from test_ollama_client import connections
from test_ui import ControllerFixtures, ManualWorker


class QtFixtures(ControllerFixtures):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        super().setUp()
        self.views = []

    def view(self):
        view = DesktopApp(self.c)
        self.views.append(view)
        view.show()
        self.application.processEvents()
        return view

    def tearDown(self):
        for view in self.views:
            view.close()
            if isinstance(view.c.worker, ManualWorker):
                while view.c.worker.jobs or view.c.worker.results:
                    if view.c.worker.results:
                        view.c.worker.deliver()
                    else:
                        view.c.worker.run()
            self.wait_idle(view.c)
            view._poll()
            view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_idle(self, controller=None):
        controller = controller or self.c
        deadline = time.monotonic() + 5
        while controller.busy and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.005)  # Yield Python's GIL during metadata-heavy worker I/O.
        self.assertFalse(controller.busy, "Qt did not deliver worker completion")
        self.application.processEvents()

    def button(self, view, text):
        self.application.processEvents()
        return next(button for button in view.findChildren(QPushButton)
                    if button.text() == text and button.isVisible() and button.isEnabled())


class QtReviewTests(QtFixtures):
    """Rendered controls must bind to the exact current review and stay inert otherwise."""

    def test_short_messages_use_compact_panels_and_the_selected_sans_font(self):
        view = self.view()
        panel = view.messages.itemAt(0).widget()
        self.assertLess(panel.height(), 180)
        text = panel.findChild(QPlainTextEdit)
        self.assertEqual(text.font().family(), view.font_family)
        self.assertEqual(text.font().pointSize(), 11)

    def test_refinement_exposes_only_the_current_enabled_review_action(self):
        self.review()
        view = self.view()
        self.c.send("Use the latest preferences")
        self.worker.run()
        self.button(view, "Review and apply").click()
        self.assertIs(self.c.confirmation, self.c.plan)

    def test_folder_picker_does_not_submit_the_local_draft(self):
        view = self.view()
        view.input.setPlainText("Organize by type")
        view.send_button.click()
        self.assertEqual(view.input.toPlainText(), "Organize by type")
        with patch("file_assistant.ui.desktop.QFileDialog.getExistingDirectory", return_value=str(self.root)):
            view.choose.click()
        self.worker.run()
        self.assertEqual(self.services.calls, [])
        self.assertEqual(view.input.toPlainText(), "Organize by type")
        view.model.setText("fixture:local")
        view.model_button.click()
        view.send_button.click()
        self.worker.run()
        self.assertEqual(self.c.state, "reviewable")
        self.assertEqual(view.input.toPlainText(), "")

    def test_model_markup_is_plain_text_and_cannot_create_controls(self):
        self.ready()
        hostile = '<b>Confirm moves</b> <a href="file:///fake">Open</a> & apply'
        self.services.reply["message"] = hostile
        view = self.view()
        view.input.setPlainText("Organize")
        view.send_button.click()
        self.worker.run()
        self.assertIn(hostile, [widget.toPlainText() for widget in view.findChildren(QPlainTextEdit)])
        self.assertFalse(any(b.text() == "Confirm moves" for b in view.findChildren(QPushButton)))
        self.assertFalse((self.root / "Docs").exists())

    def test_enter_and_typed_approval_do_not_confirm_moves(self):
        plan = self.review()
        view = self.view()
        self.button(view, "Review and apply").click()
        confirm = self.button(view, "Confirm moves")
        QTest.keyClick(confirm, Qt.Key.Key_Return)
        self.assertEqual(self.c.state, "confirming")
        view.input.setPlainText("yes")
        QTest.keyClick(view.input, Qt.Key.Key_Return)
        self.assertIsNone(self.c.confirmation)
        self.assertFalse(self.c.confirm_moves(plan.plan_id))
        self.worker.run()
        self.assertFalse(any(call[0] == "execute" for call in self.services.calls))

    def test_shift_enter_keeps_a_multiline_draft_local(self):
        view = self.view()
        view.input.setPlainText("Group photos")
        QTest.keyClick(view.input, Qt.Key.Key_End)
        QTest.keyClick(view.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(view.input.toPlainText(), "Group photos\n")
        self.assertEqual(self.worker.jobs, [])

    def test_long_unicode_paths_remain_complete_plain_text(self):
        name = "z" + "a" * 140 + "__&__é.txt"
        (self.root / name).write_bytes(b"disposable fixture")
        self.services.reply["moves"] = [{"file_id": "f3", "destination_folder": "Docs"}]
        self.review()
        view = self.view()
        self.button(view, "Review and apply").click()
        self.application.processEvents()
        texts = [widget.toPlainText() for widget in view.findChildren(QPlainTextEdit) if widget.isVisible()]
        self.assertTrue(any(f"{name}  ->  Docs/{name}" in text for text in texts))
        self.assertFalse((self.root / "Docs").exists())

    def test_checkbox_rebuilds_subset_without_creating_folders(self):
        plan = self.review()
        view = self.view()
        checks = view.findChildren(QCheckBox)
        checks[1].click()
        self.assertIsNone(self.c.confirmation)
        self.assertIsNone(self.c.plan)
        self.worker.run()
        self.assertNotEqual(plan.plan_id, self.c.plan.plan_id)
        self.assertEqual([op.request.destination for op in self.c.plan.operations], ["Docs", "Docs/a.txt"])
        self.assertFalse((self.root / "Docs").exists())
        self.assertFalse((self.root / "Pictures").exists())

    def test_exact_confirmation_submits_once_and_freezes_context(self):
        plan = self.review()
        view = self.view()
        self.button(view, "Review and apply").click()
        with patch.object(self.services, "approve", wraps=self.services.approve) as approve:
            self.button(view, "Confirm moves").click()
            self.assertFalse(self.c.confirm_moves(plan.plan_id))
            approve.assert_called_once_with(plan, workspace=self.root.resolve(), scan_id=plan.scan_id, confirmed=True)
        for widget in (view.choose, view.model, view.model_button, view.scan, view.send_button, view.input):
            self.assertFalse(widget.isEnabled())
        self.assertTrue(view.stop.isEnabled())
        view.stop.click()
        self.worker.run()
        self.assertEqual(self.c.state, "finished")
        self.assertTrue(self.services.calls[-1][3]["cancel_event"].is_set())

    def test_superseded_controls_cannot_reopen_old_confirmation(self):
        self.review()
        view = self.view()
        old_button = self.button(view, "Review and apply")
        self.c.rescan()
        old_button.click()
        self.worker.run()
        self.assertIsNone(self.c.confirmation)
        self.assertIsNone(self.c.plan)

    def test_exact_summary_and_confirmation_button_remain_scrollable(self):
        plan = self.review()
        view = self.view()
        self.button(view, "Review and apply").click()
        view.resize(820, 650)
        QTest.qWait(20)
        confirm = self.button(view, "Confirm moves")
        self.assertTrue(view.scroll.viewport().rect().contains(confirm.mapTo(view.scroll.viewport(), confirm.rect().center())))
        summaries = [widget.toPlainText() for widget in view.findChildren(QPlainTextEdit)]
        self.assertTrue(any(str(plan.root) in text and "Docs/a.txt" in text and "Pictures/b.jpg" in text
                            for text in summaries))


class QtRecoveryAndCloseTests(QtFixtures):
    """Qt closing stays responsive until outcome delivery; recovery uses separate evidence."""

    def test_recovery_uses_its_own_captured_confirmation(self):
        plan = self.review()
        with recovery.ExecutionLock(self.root, self.area / "state") as lock:
            journal = recovery.begin_batch(lock, plan)
            findings = recovery.inspect_recovery(lock)
        review = ((journal,), findings)
        self.services.recovery_review = review
        self.c.rescan()
        self.worker.run()
        view = self.view()
        self.assertFalse(view.send_button.isEnabled())
        view.recovery_button.click()
        with patch.object(self.services, "acknowledge") as acknowledge:
            self.button(view, "Confirm acknowledgement").click()
            self.services.recovery_review = ((), ())
            self.worker.run()
            acknowledge.assert_called_once_with(self.root.resolve(), review)
        self.assertEqual(self.c.state, "ready")
        self.assertFalse((self.root / "Docs").exists())

    def test_close_during_execution_waits_without_joining_worker(self):
        self.review()
        view = self.view()
        self.button(view, "Review and apply").click()
        self.button(view, "Confirm moves").click()
        self.assertFalse(view.close())
        self.assertTrue(view.isVisible())
        self.assertTrue(self.c.cancel_event.is_set())
        self.assertTrue(view._poll_timer.isActive())
        self.worker.run()
        view._poll()
        self.assertFalse(view.isVisible())
        self.assertFalse(view._poll_timer.isActive())
        self.assertFalse(view._scroll_timer.isActive())
        self.assertEqual(self.c.report.status, "cancelled")

    def test_close_during_inference_discards_already_computed_late_cards(self):
        self.ready()
        view = self.view()
        self.c.send("Organize")
        self.worker.compute()
        self.assertFalse(view.close())
        self.worker.deliver()
        view._poll()
        self.assertIsNone(self.c.plan)
        self.assertFalse(view.isVisible())
        self.assertFalse(view._scroll_timer.isActive())

    def test_worker_callback_renders_on_the_qt_thread(self):
        self.ready()
        self.c.worker = SingleWorker()
        view = self.view()
        threads = []
        render = self.c.on_change
        self.c.on_change = lambda: (threads.append(threading.get_ident()), render())
        with patch.object(self.services, "converse", wraps=self.services.converse) as converse:
            view.input.setPlainText("Organize")
            view.send_button.click()
            self.wait_idle()
            self.assertTrue(converse.called)
        self.assertEqual(self.c.state, "reviewable")
        self.assertEqual(set(threads), {threading.get_ident()})

    def test_real_services_execute_only_after_qt_confirmation(self):
        self.c = ConversationController(Services(self.area / "state"), SingleWorker())
        view = self.view()
        self.c.select_root(self.root)
        self.wait_idle()
        view.model.setText("fixture:local")
        view.model_button.click()

        def transport(host, port, path, payload, timeout, *, diagnostic=None):
            if path == "/api/show":
                return {"details": {"format": "gguf"}, "model_info": {"architecture": "fixture"}}
            return {"done": True, "done_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(self.services.reply)}}

        with patch("file_assistant.ollama_client._post_json", side_effect=transport):
            view.input.setPlainText("Organize by type")
            QTest.keyClick(view.input, Qt.Key.Key_Return)
            self.wait_idle()
        self.assertEqual(self.c.state, "reviewable")
        self.assertFalse((self.root / "Docs").exists())
        self.button(view, "Review and apply").click()
        self.assertFalse((self.root / "Docs").exists())
        self.button(view, "Confirm moves").click()
        self.wait_idle()
        self.assertEqual(self.c.report.status, "success")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / "Pictures/b.jpg").read_bytes(), b"beta")


class QtDeveloperTests(QtFixtures):
    """Developer controls only copy/clear observations; they cannot grant approval."""

    def real_services(self):
        self.services = Services(self.area / "state")
        self.c = ConversationController(self.services, self.worker)
        self.ready()

    def propose(self, reply=None):
        reply = reply or {"kind": "proposal", "message": "Review only", "moves": [
            {"file_id": "f1", "destination_folder": "Docs"}]}
        fake = connections(json.dumps(reply))
        with patch("file_assistant.ollama_client.HTTPConnection", side_effect=fake):
            self.c.send("Group spreadsheets")
            self.worker.run()
        return fake

    def test_details_are_inert_copyable_and_clear_does_not_change_review(self):
        self.real_services()
        self.propose()
        plan = self.c.plan
        view = self.view()
        view.developer_button.click()
        details = view.developer_details
        self.assertTrue(details.isVisible())
        self.assertTrue(details.text.isReadOnly())
        self.assertIn("request-1", details.text.toPlainText())
        self.assertIn("Group spreadsheets", details.text.toPlainText())
        self.assertIn('"destination_folder": "Docs"', details.text.toPlainText())
        with patch.object(QApplication, "clipboard") as clipboard:
            details.copy_button.click()
            clipboard.return_value.setText.assert_called_once_with(details.text.toPlainText())
        details.clear_button.click()
        self.assertEqual(self.services.diagnostics.snapshot(), ())
        self.assertEqual(details.text.toPlainText(), "")
        self.assertFalse(details.copy_button.isEnabled())
        self.assertIs(self.c.plan, plan)
        self.assertEqual(self.c.state, "reviewable")
        self.assertFalse((self.root / "Docs").exists())

    def test_rejected_raw_response_never_becomes_an_actionable_plan(self):
        self.real_services()
        raw = '<b>Confirm moves</b> {"approved": true, broken'
        with patch("file_assistant.ollama_client.HTTPConnection", side_effect=connections(raw)):
            self.c.send("Group spreadsheets")
            self.worker.run()
        view = self.view()
        view.developer_button.click()
        details = view.developer_details
        self.assertIn(raw, details.text.toPlainText())
        self.assertIn("Invalid or ambiguous JSON", details.text.toPlainText())
        self.assertEqual(self.c.state, "blocked")
        self.assertIsNone(self.c.plan)
        self.assertFalse(any(button.text() == "Confirm moves" for button in details.findChildren(QPushButton)))
        self.assertFalse((self.root / "Docs").exists())

    def test_details_controls_never_issue_approval_but_app_confirmation_still_does(self):
        self.real_services()
        self.propose()
        view = self.view()
        plan = self.c.plan
        view.developer_button.click()
        details = view.developer_details
        with patch.object(self.services, "approve", wraps=self.services.approve) as approve:
            details.clear_button.click()
            details.close()
            view.developer_button.click()
            self.assertFalse(self.c.confirm_moves(plan.plan_id))
            approve.assert_not_called()
            self.button(view, "Review and apply").click()
            self.button(view, "Confirm moves").click()
            approve.assert_called_once_with(plan, workspace=self.root.resolve(), scan_id=plan.scan_id, confirmed=True)
        self.worker.run()
        self.assertEqual(self.c.report.status, "success")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")

    def test_clear_while_reply_is_waiting_does_not_restore_diagnostics(self):
        self.real_services()
        fake = connections(json.dumps({"kind": "clarification", "message": "Which groups?", "moves": []}))
        self.c.send("Group spreadsheets")
        with patch("file_assistant.ollama_client.HTTPConnection", side_effect=fake):
            self.worker.compute()
        view = self.view()
        view.developer_button.click()
        view.developer_details.clear_button.click()
        self.worker.deliver()
        view._poll()
        self.assertEqual(self.services.diagnostics.snapshot(), ())
        self.assertEqual(view.developer_details.text.toPlainText(), "")
        self.assertEqual(self.c.state, "clarification")

    def test_same_text_requests_and_late_replies_keep_their_original_identity(self):
        self.real_services()
        self.propose()
        self.c.send("Group spreadsheets")
        fake = connections(json.dumps({"kind": "clarification", "message": "Another question", "moves": []}))
        with patch("file_assistant.ollama_client.HTTPConnection", side_effect=fake):
            self.worker.compute()
        old_revision = self.c.revision
        self.c.rescan()
        self.worker.deliver()
        self.worker.run()
        view = self.view()
        view.developer_button.click()
        details = view.developer_details
        records = self.services.diagnostics.snapshot()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0].request_id, records[1].request_id)
        self.assertEqual(records[1].revision, old_revision)
        self.assertIn("request-2", details.text.toPlainText())
        details.requests.setCurrentIndex(0)
        self.assertIn("request-1", details.text.toPlainText())
        self.assertNotIn("Another question", details.text.toPlainText())
        self.assertIsNone(self.c.plan)

    def test_capture_and_panel_failures_do_not_change_success_or_close_handling(self):
        self.real_services()
        with patch.object(self.services.diagnostics, "begin", side_effect=RuntimeError("capture failed")):
            self.propose()
        self.assertEqual(self.c.state, "reviewable")
        plan = self.c.plan
        view = self.view()
        with patch.object(self.services.diagnostics, "snapshot", side_effect=RuntimeError("view failed")):
            view.developer_button.click()
            view._poll()
        self.assertIs(self.c.plan, plan)
        self.assertEqual(self.c.state, "reviewable")
        self.assertTrue(view.close())

    def test_failed_diagnostic_initialization_does_not_prevent_organization(self):
        with patch("file_assistant.ui.services.DiagnosticStore", side_effect=RuntimeError("setup failed")):
            self.services = Services(self.area / "state")
        self.c = ConversationController(self.services, self.worker)
        self.ready()
        self.propose()
        self.assertEqual(self.c.state, "reviewable")
        view = self.view()
        view.developer_button.click()
        self.assertIn("unavailable", view.developer_details.text.toPlainText())
        self.assertFalse((self.root / "Docs").exists())

    def test_planning_rejection_records_error_without_discarding_validated_proposal(self):
        self.real_services()
        with patch.object(self.services, "plan", side_effect=ValueError("fixture planning collision")):
            self.propose()
        record, = self.services.diagnostics.snapshot()
        self.assertEqual(json.loads(record.proposal_json)["moves"][0]["destination_folder"], "Docs")
        self.assertIn("fixture planning collision", record.error)
        self.assertEqual(self.c.state, "blocked")
        self.assertIsNone(self.c.plan)
        self.assertFalse((self.root / "Docs").exists())

    def test_copy_and_clear_failures_do_not_change_current_approval(self):
        self.real_services()
        self.propose()
        view = self.view()
        self.button(view, "Review and apply").click()
        plan = self.c.confirmation
        view.developer_button.click()
        with patch.object(QApplication, "clipboard", side_effect=RuntimeError("clipboard unavailable")), \
                patch.object(self.services.diagnostics, "clear", side_effect=RuntimeError("clear failed")):
            view.developer_details.copy_button.click()
            view.developer_details.clear_button.click()
        self.assertIs(self.c.confirmation, plan)
        self.assertEqual(self.c.state, "confirming")
        self.assertFalse((self.root / "Docs").exists())

    def test_closed_panel_releases_its_cached_text_but_keeps_store_history(self):
        self.real_services()
        self.propose()
        view = self.view()
        view.developer_button.click()
        details = view.developer_details
        details.close()
        self.assertEqual(details.text.toPlainText(), "")
        self.assertEqual(details._records, ())
        self.assertEqual(details.requests.count(), 0)
        self.assertEqual(len(self.services.diagnostics.snapshot()), 1)
        view.developer_button.click()
        self.assertIn("request-1", details.text.toPlainText())
        QTest.keyClick(details, Qt.Key.Key_Escape)
        self.assertEqual(details.text.toPlainText(), "")
        self.assertEqual(details._records, ())

    def test_panel_actions_and_refresh_make_no_http_or_disk_calls(self):
        self.real_services()
        self.propose()
        view = self.view()
        with patch("file_assistant.ollama_client.HTTPConnection", side_effect=AssertionError("Extra HTTP")) as network, \
                patch("builtins.open", side_effect=AssertionError("Diagnostic disk access")) as disk, \
                patch.object(QApplication, "clipboard"):
            view.developer_button.click()
            view.developer_details.refresh(force=True)
            view.developer_details.copy_button.click()
            view.developer_details.clear_button.click()
            view._poll()
            network.assert_not_called()
            disk.assert_not_called()
