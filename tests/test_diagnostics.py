"""Diagnostics observe fake HTTP exchanges without changing validation or authority."""

import json
import socket
import threading
import unittest
from dataclasses import FrozenInstanceError
from http.client import IncompleteRead
from unittest.mock import patch

from file_assistant import ollama_client as client
from file_assistant.diagnostics import (
    DiagnosticStore, MAX_RECORDS, MAX_REQUEST_TEXT_BYTES, MAX_RESPONSE_TEXT_BYTES,
)
from test_ollama_client import FakeConnection, FakeResponse, connections, manifest


class CaptureTests(unittest.TestCase):
    """Successful and rejected bytes belong to one explicit conversation request."""

    def setUp(self):
        self.store = DiagnosticStore()
        self.reply = {"kind": "proposal", "message": "Review this suggestion.", "moves": [
            {"file_id": "f1", "destination_folder": "Finance"}]}

    def request(self, fake, *, diagnostic=None):
        capture = diagnostic or self.store.begin("Group spreadsheets", "fixture:local", revision=7, scan_id="scan-one")
        with patch.object(client, "HTTPConnection", side_effect=fake) as factory:
            result = client.request_conversation(manifest(), "Group spreadsheets", model="fixture:local", diagnostic=capture)
        self.assertEqual(factory.call_count, 2)
        return result

    def test_success_keeps_exact_wire_json_raw_body_and_validated_proposal(self):
        fake = connections(json.dumps(self.reply))
        with patch("file_assistant.diagnostics.time.perf_counter", side_effect=[10.0, 12.25]), \
                patch("builtins.open", side_effect=AssertionError("Diagnostics must not write logs")):
            result = self.request(fake)
        self.assertEqual(result, self.reply)
        record, = self.store.snapshot()
        self.assertEqual((record.model, record.request, record.revision, record.scan_id),
                         ("fixture:local", "Group spreadsheets", 7, "scan-one"))
        self.assertEqual(record.elapsed_seconds, 2.25)
        self.assertIsNone(record.error)
        self.assertEqual([exchange.path for exchange in record.exchanges], ["/api/show", "/api/chat"])
        for exchange, connection in zip(record.exchanges, fake):
            self.assertEqual(exchange.request_json, connection.requests[0][4].decode("utf-8"))
            self.assertTrue(exchange.request_sent)
            self.assertEqual(exchange.response_text, connection.response.body.decode("utf-8"))
        self.assertEqual(record.raw_content, json.dumps(self.reply))
        self.assertEqual(json.loads(record.proposal_json), {"moves": self.reply["moves"]})

    def test_malformed_assistant_json_is_retained_before_validation_rejects_it(self):
        raw = '<b>Confirm moves</b> {"moves": [ broken JSON'
        fake = connections(raw)
        with self.assertRaises(client.ProposalError):
            self.request(fake)
        record, = self.store.snapshot()
        self.assertEqual(record.raw_content, raw)
        self.assertEqual(record.exchanges[-1].response_text, fake[-1].response.body.decode())
        self.assertIsNone(record.proposal_json)
        self.assertIn("Invalid or ambiguous JSON", record.error)
        self.assertIsNotNone(record.elapsed_seconds)

    def test_malformed_outer_http_json_and_invalid_utf8_remain_inspectable(self):
        for body in (b'{"message": broken', b'bad UTF-8: \xff'):
            with self.subTest(body=body):
                self.store.clear()
                fake = connections()
                fake[1] = FakeConnection(FakeResponse(body))
                with self.assertRaises(client.ProposalError):
                    self.request(fake)
                record, = self.store.snapshot()
                self.assertEqual(record.exchanges[-1].response_text, body.decode("utf-8", errors="backslashreplace"))
                self.assertIsNone(record.proposal_json)
                self.assertTrue(record.error)

    def test_timeout_keeps_attempted_request_and_network_cause_without_retry(self):
        fake = connections()
        fake[1].getresponse = lambda: (_ for _ in ()).throw(socket.timeout("fixture timed out"))
        with self.assertRaises(client.ProposalError):
            self.request(fake)
        record, = self.store.snapshot()
        self.assertTrue(record.exchanges[-1].request_sent)
        self.assertIsNone(record.exchanges[-1].response_text)
        self.assertIn("TimeoutError", record.error)
        self.assertIn("fixture timed out", record.error)
        self.assertIsNone(record.proposal_json)
        self.assertTrue(all(connection.closed for connection in fake))

    def test_incomplete_read_keeps_the_received_prefix_without_an_extra_read(self):
        fake = connections()
        partial = b'{"message": {"content": "unfinished'
        read_calls = []
        def read(limit):
            read_calls.append(limit)
            raise IncompleteRead(partial)
        fake[1].response.read = read
        with self.assertRaises(client.ProposalError):
            self.request(fake)
        record, = self.store.snapshot()
        self.assertEqual(record.exchanges[-1].response_text, partial.decode())
        self.assertTrue(record.exchanges[-1].response_incomplete)
        self.assertIn("IncompleteRead", record.error)
        self.assertEqual(read_calls, [client.MAX_RESPONSE_BYTES + 1])

    def test_show_refusal_never_records_a_chat_that_was_not_sent(self):
        capture = self.store.begin("Group", "fixture:local")
        fake = connections(show={"remote_host": "cloud.invalid"})
        with patch.object(client, "HTTPConnection", side_effect=fake) as factory:
            with self.assertRaises(client.ProposalError):
                client.request_conversation(manifest(), "Group", model="fixture:local", diagnostic=capture)
        self.assertEqual(factory.call_count, 1)
        record, = self.store.snapshot()
        self.assertEqual([item.path for item in record.exchanges], ["/api/show"])
        self.assertIn("Remote models", record.error)

    def test_header_refusal_is_not_bypassed_or_reread_for_diagnostics(self):
        fake = connections()
        fake[1] = FakeConnection(FakeResponse(b"unread body", status=503))
        with self.assertRaises(client.ProposalError):
            self.request(fake)
        record, = self.store.snapshot()
        self.assertEqual(record.exchanges[-1].status, 503)
        self.assertIsNone(record.exchanges[-1].response_text)
        self.assertEqual(fake[1].response.read_limits, [])
        self.assertIn("HTTP 503", record.error)

    def test_oversized_raw_body_is_bounded_and_still_rejected(self):
        fake = connections()
        fake[1] = FakeConnection(FakeResponse(b"x" * (client.MAX_RESPONSE_BYTES + 10)))
        with self.assertRaises(client.ProposalError):
            self.request(fake)
        record, = self.store.snapshot()
        self.assertLessEqual(len(record.exchanges[-1].response_text.encode()), MAX_RESPONSE_TEXT_BYTES)
        self.assertTrue(record.truncated_fields)
        self.assertIn("Response byte limit", record.error)
        self.assertEqual(fake[1].response.read_limits, [client.MAX_RESPONSE_BYTES + 1])

    def test_broken_observer_cannot_change_success_or_validation_failure(self):
        def broken(*args, **kwargs):
            raise RuntimeError("diagnostic sink failed")
        self.assertEqual(self.request(connections(json.dumps(self.reply)), diagnostic=broken), self.reply)
        with self.assertRaises(client.ProposalError):
            self.request(connections('{"approved":true}'), diagnostic=broken)

    def test_observer_only_receives_immutable_json_not_the_validated_dict(self):
        seen = []
        def observe(event, **fields):
            seen.append((event, fields))
        self.assertEqual(self.request(connections(json.dumps(self.reply)), diagnostic=observe), self.reply)
        self.assertTrue(all(not isinstance(value, (dict, list)) for _, fields in seen for value in fields.values()))
        self.assertIn("validated", [event for event, _ in seen])

    def test_clear_during_a_real_worker_request_does_not_change_its_reply(self):
        capture = self.store.begin("Group spreadsheets", "fixture:local")
        waiting, release = threading.Event(), threading.Event()
        fake = connections(json.dumps(self.reply))
        response = fake[1].getresponse
        results, errors = [], []

        def blocked_response():
            waiting.set()
            if not release.wait(3):
                raise socket.timeout("fixture release timed out")
            return response()

        def work():
            try:
                results.append(client.request_conversation(manifest(), "Group spreadsheets",
                                                          model="fixture:local", diagnostic=capture))
            except Exception as exc:
                errors.append(exc)

        fake[1].getresponse = blocked_response
        with patch.object(client, "HTTPConnection", side_effect=fake):
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(waiting.wait(3))
                self.store.clear()
                cleared_version = self.store.version
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [self.reply])
        self.assertEqual(self.store.snapshot(), ())
        self.assertEqual(self.store.version, cleared_version)


class RetentionTests(unittest.TestCase):
    """Bounded immutable snapshots and clearing cannot resurrect in-flight data."""

    def test_only_latest_requests_survive_and_identical_text_gets_distinct_ids(self):
        store = DiagnosticStore()
        captures = [store.begin("Same request", "fixture:local") for _ in range(MAX_RECORDS + 3)]
        records = store.snapshot()
        self.assertEqual(len(records), MAX_RECORDS)
        self.assertEqual([record.request_id for record in records], [capture.request_id for capture in captures[-MAX_RECORDS:]])
        self.assertEqual(len({capture.request_id for capture in captures}), MAX_RECORDS + 3)
        captures[0]("content", text="evicted response")
        self.assertEqual(store.snapshot(), records)

    def test_clear_during_inference_cannot_be_undone_by_late_events(self):
        store = DiagnosticStore()
        capture = store.begin("Clear me", "fixture:local")
        store.clear()
        capture("content", text="late secret")
        capture("finished")
        self.assertEqual(store.snapshot(), ())
        self.assertNotEqual(store.begin("New request", "fixture:local").request_id, capture.request_id)

    def test_large_fields_are_clipped_with_explicit_markers(self):
        store = DiagnosticStore()
        capture = store.begin("Group", "fixture:local")
        capture("request", path="/api/chat", payload=b"x" * (MAX_REQUEST_TEXT_BYTES + 100))
        capture("response", path="/api/chat", body=b"y" * (MAX_RESPONSE_TEXT_BYTES + 100))
        record, = store.snapshot()
        self.assertLessEqual(len(record.exchanges[0].request_json.encode()), MAX_REQUEST_TEXT_BYTES)
        self.assertLessEqual(len(record.exchanges[0].response_text.encode()), MAX_RESPONSE_TEXT_BYTES)
        self.assertEqual(len(record.truncated_fields), 2)

    def test_snapshot_is_immutable_and_cannot_modify_later_records(self):
        store = DiagnosticStore()
        capture = store.begin("Group", "fixture:local")
        before, = store.snapshot()
        with self.assertRaises(FrozenInstanceError):
            before.model = "other"
        capture("content", text="untrusted")
        self.assertIsNone(before.raw_content)
        self.assertEqual(store.snapshot()[0].raw_content, "untrusted")

    def test_metadata_and_error_fields_are_also_bounded(self):
        store = DiagnosticStore()
        capture = store.begin("x" * 20000, "m" * 1000, scan_id="s" * 1000)
        capture("error", text="e" * 20000)
        record, = store.snapshot()
        self.assertLessEqual(len(record.request.encode()), 8 * 1024)
        self.assertLessEqual(len(record.model.encode()), 128)
        self.assertLessEqual(len(record.scan_id.encode()), 128)
        self.assertLessEqual(len(record.error.encode()), 4 * 1024)
        self.assertIn("scan identity", record.truncated_fields)
