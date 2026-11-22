"""Disposable journal fixtures and fake outcomes; no executor is implemented."""

import io
import ctypes
import json
import multiprocessing
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from file_assistant.operations import build_organization_plan, scan_files
from file_assistant.models import PathObservation
from file_assistant.recovery import (
    ExecutionLock, LockUnavailable, PersistenceError, RecoveryRequired,
    begin_batch, finish_unexecuted, inspect_recovery, is_complete, load_journals,
    mark_attempted, record_intent, record_outcome, resolve_recovery,
)


def lock_process(root, state, connection, abrupt=False):
    """Use a distinct process to probe contention or exit without cleanup."""
    try:
        with ExecutionLock(Path(root), Path(state)):
            connection.send("locked")
            connection.recv()
            if abrupt:
                os._exit(0)
    except LockUnavailable:
        connection.send("busy")
    finally:
        connection.close()


@unittest.skipUnless(os.name == "nt", "Windows journal/locking tests")
class RecoveryFixtures(unittest.TestCase):
    """Every managed path and app-state location is a disposable fixture."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        self.root = self.area / "workspace"
        self.root.mkdir()
        (self.root / "alpha.txt").write_bytes(b"synthetic fixture")
        (self.root / "Documents").mkdir()
        self.state = self.area / "state"
        self.plan = self.make_plan()
        self.operation = self.plan.operations[-1]

    def make_plan(self, folder="Documents"):
        snapshot = scan_files(self.root)
        return build_organization_plan(
            self.root, snapshot,
            {"moves": [{"file_id": snapshot.files[0].file_id, "destination_folder": folder}]},
            proposal_scan_id=snapshot.scan_id,
        )

    def path(self):
        return self.state / f"journal-{self.plan.plan_id}.json"

    def intent(self, lock):
        begin_batch(lock, self.plan)
        return record_intent(lock, self.plan.plan_id, self.operation.plan_id)

    def observed(self, relative):
        path = self.root / relative
        details, parent = path.lstat(), path.parent.lstat()
        return PathObservation(
            relative, True, details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, "directory" if path.is_dir() else "file", details.st_nlink,
            parent.st_dev, parent.st_ino,
        )

    def finding(self, lock):
        return inspect_recovery(lock)[0]

    def start_process(self, root=None, abrupt=False):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=lock_process,
                                  args=(str(root or self.root), str(self.state), child, abrupt))
        process.start()
        child.close()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(10)
            parent.close()

        self.addCleanup(cleanup)
        self.assertTrue(parent.poll(15), "lock child did not respond")
        return process, parent, parent.recv()


class JournalPersistenceTests(RecoveryFixtures):
    """Durable records precede effects and survive failed outcome updates."""

    def test_default_state_directory_matches_configuration(self):
        """Journal and lock state default to the existing FileAssistant directory."""
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.area)}):
            with ExecutionLock(self.root) as lock:
                begin_batch(lock, self.plan)
        self.assertTrue((self.area / "FileAssistant" / self.path().name).is_file())

    def test_missing_journal_is_distinct_from_pending_and_intent(self):
        """No record yields empty results; unfinished records retain their evidence."""
        with ExecutionLock(self.root, self.state) as lock:
            self.assertEqual(load_journals(lock), ())
            self.assertEqual(inspect_recovery(lock), ())
            journal = begin_batch(lock, self.plan)
            self.assertEqual(journal.plan, self.plan)
            self.assertEqual(journal.records[0].status, "pending")
            self.assertEqual(self.finding(lock).conclusion, "not_started")
            record_intent(lock, self.plan.plan_id, self.operation.plan_id)
            loaded = load_journals(lock)[0]
            self.assertEqual(loaded.records[0].status, "intent")
            self.assertEqual(loaded.records[0].intent_observations, self.operation.observations)
        with ExecutionLock(self.root, self.state) as lock:
            self.assertEqual(load_journals(lock)[0], loaded)

    def test_success_outcome_retains_identity_and_prevents_replay(self):
        """Retain completed journals and reject a second begin for the same batch."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
            outcome = self.observed("Documents/alpha.txt")
            record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "success",
                           observations=(outcome,), message="synthetic move outcome")
            loaded = load_journals(lock)[0]
            self.assertEqual(loaded.records[0].outcome_observations, (outcome,))
            self.assertEqual(inspect_recovery(lock), ())
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, self.plan)

    def test_completed_batch_allows_a_distinct_new_batch(self):
        """Separate completed records are retained when a new batch is started."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
            record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "success",
                           observations=(self.observed("Documents/alpha.txt"),))
            empty = replace(self.plan, plan_id="a" * 32, operations=())
            begin_batch(lock, empty)
            self.assertEqual(len(load_journals(lock)), 2)

    def test_unresolved_batch_blocks_any_new_batch_without_changing_evidence(self):
        """Never replace a pending, failed, uncertain or interrupted journal."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            before = self.path().read_bytes()
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, replace(self.plan, plan_id="a" * 32))
            self.assertEqual(self.path().read_bytes(), before)

    def test_failure_and_uncertainty_block_further_intents(self):
        """A non-success result stops execution and remains recovery-required."""
        for status in ("failed", "recovery_required"):
            with self.subTest(status=status), TemporaryDirectory() as temp:
                with ExecutionLock(self.root, Path(temp)) as lock:
                    self.intent(lock)
                    record_outcome(lock, self.plan.plan_id, self.operation.plan_id,
                                   status, message="simulated permission/outcome failure")
                    with self.assertRaises(RecoveryRequired):
                        record_intent(lock, self.plan.plan_id, self.operation.plan_id)
                    with self.assertRaises(RecoveryRequired):
                        begin_batch(lock, replace(self.plan, plan_id="b" * 32))

    def test_failed_initial_persistence_must_not_start_execution(self):
        """A batch write failure raises explicitly and leaves no successful journal."""
        with ExecutionLock(self.root, self.state) as lock:
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(PersistenceError):
                    begin_batch(lock, self.plan)
            self.assertFalse(self.path().exists())
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, self.plan)

    def test_intent_fsync_or_replacement_failure_preserves_pending_record(self):
        """No operation may start after intent cannot be durably persisted."""
        for target in ("os.fsync", "os.replace"):
            with self.subTest(target=target), TemporaryDirectory() as temp:
                state = Path(temp)
                with ExecutionLock(self.root, state) as lock:
                    begin_batch(lock, self.plan)
                    path = state / self.path().name
                    before = path.read_bytes()
                    with patch("file_assistant.recovery." + target, side_effect=OSError("failure")):
                        with self.assertRaisesRegex(PersistenceError, "must not start"):
                            record_intent(lock, self.plan.plan_id, self.operation.plan_id)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(load_journals(lock)[0].records[0].status, "pending")
                    self.assertEqual(list(state.glob("*.tmp")), [])

    def test_outcome_failure_preserves_durable_intent_and_stops_session(self):
        """After a fake move, a failed outcome write preserves uncertainty."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            before = self.path().read_bytes()
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("failure")):
                with self.assertRaisesRegex(PersistenceError, "uncertain"):
                    record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "success",
                                   observations=(self.observed("Documents/alpha.txt"),))
            self.assertEqual(self.path().read_bytes(), before)
            self.assertEqual(self.finding(lock).conclusion, "destination_only")
            with self.assertRaises(RecoveryRequired):
                record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "failed")

    def test_flush_fsync_precede_atomic_replace(self):
        """Flush/fsync the temporary file and close it before atomic replacement."""
        events = []
        original_fsync, original_replace = os.fsync, os.replace

        def synced(fd):
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(json.loads(os.read(fd, 1024 * 1024))["version"], 1)
            events.append("fsync")
            return original_fsync(fd)

        def replaced(source, destination):
            events.append("replace")
            return original_replace(source, destination)

        with ExecutionLock(self.root, self.state) as lock:
            with patch("file_assistant.recovery.os.fsync", side_effect=synced), \
                    patch("file_assistant.recovery.os.replace", side_effect=replaced):
                begin_batch(lock, self.plan)
        self.assertEqual(events, ["fsync", "replace"])

    def test_operations_require_a_live_lock(self):
        """A closed or never-entered lock cannot read or update execution state."""
        lock = ExecutionLock(self.root, self.state)
        for function in (load_journals, inspect_recovery):
            with self.assertRaises(RecoveryRequired):
                function(lock)
        with lock:
            begin_batch(lock, self.plan)
        with self.assertRaises(RecoveryRequired):
            record_intent(lock, self.plan.plan_id, self.operation.plan_id)

    def test_restart_can_inspect_but_cannot_resume_old_execution(self):
        """A new lock session cannot append intent/outcomes to interrupted work."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
        with ExecutionLock(self.root, self.state) as lock:
            self.assertEqual(self.finding(lock).conclusion, "source_only")
            before = self.path().read_bytes()
            with self.assertRaises(RecoveryRequired):
                record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "failed")
            self.assertEqual(self.path().read_bytes(), before)

    def test_write_failure_preserves_durable_record(self):
        """A short/failed temporary write cannot replace the pending journal."""
        original = tempfile.NamedTemporaryFile
        for mode in ("failed", "short", "flush"):
            with self.subTest(mode=mode), TemporaryDirectory() as temp:
                state = Path(temp)
                with ExecutionLock(self.root, state) as lock:
                    begin_batch(lock, self.plan)
                    path = state / self.path().name
                    before = path.read_bytes()

                    def failed_stream(*args, **kwargs):
                        stream = original(*args, **kwargs)
                        if mode == "failed":
                            stream.write = Mock(side_effect=OSError("synthetic write error"))
                        elif mode == "short":
                            stream.write = Mock(return_value=0)
                        else:
                            original_flush = stream.flush
                            calls = 0

                            def failed_flush():
                                nonlocal calls
                                calls += 1
                                if calls == 1:
                                    raise OSError("synthetic flush error")
                                return original_flush()

                            stream.flush = failed_flush
                        return stream

                    with patch("file_assistant.recovery.tempfile.NamedTemporaryFile", side_effect=failed_stream):
                        with self.assertRaises(PersistenceError):
                            record_intent(lock, self.plan.plan_id, self.operation.plan_id)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(list(state.glob("*.tmp")), [])

    def test_outcome_fsync_failure_retains_previous_intent(self):
        """A failed flush to storage after an effect cannot publish a false outcome."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            before = self.path().read_bytes()
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
            with patch("file_assistant.recovery.os.fsync", side_effect=OSError("flush failure")):
                with self.assertRaises(PersistenceError):
                    record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "success",
                                   observations=(self.observed("Documents/alpha.txt"),))
            self.assertEqual(self.path().read_bytes(), before)


class JournalValidationTests(RecoveryFixtures):
    """Loaded JSON is untrusted and must never choose unsafe filesystem paths."""

    def test_overlap_is_rejected_before_creating_state(self):
        """Reject state inside, equal to or containing the managed root."""
        for state in (self.root / "state", self.root, self.area):
            with self.subTest(state=state), self.assertRaises((ValueError, RecoveryRequired)):
                with ExecutionLock(self.root, state):
                    self.fail("overlap accepted")
        self.assertFalse((self.root / "state").exists())

    def test_redirected_state_ancestor_is_rejected_before_creation(self):
        """App-owned directories must never be created through a redirect."""
        with patch.object(Path, "is_junction", new=lambda path: path == self.area):
            with self.assertRaises(RecoveryRequired):
                with ExecutionLock(self.root, self.state):
                    self.fail("state redirect accepted")
        self.assertFalse(self.state.exists())

    def test_short_path_alias_cannot_hide_state_overlap(self):
        """Resolve an existing Windows alias before creating a state subdirectory."""
        root = self.area / "LongManagedDirectoryNameForAlias"
        root.mkdir()
        lookup = ctypes.windll.kernel32.GetShortPathNameW
        lookup.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        lookup.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = lookup(str(root), buffer, len(buffer))
        if not length or length >= len(buffer) or Path(buffer.value).name.casefold() == root.name.casefold():
            self.skipTest("8.3 aliases unavailable on this volume")
        alias = Path(buffer.value)
        for workspace, state in ((alias, root / "state"), (root, alias / "state")):
            with self.subTest(workspace=workspace), self.assertRaises(RecoveryRequired):
                with ExecutionLock(workspace, state):
                    self.fail("aliased overlap accepted")
            self.assertFalse((root / "state").exists())

    def test_corrupt_records_are_never_treated_as_no_journal(self):
        """Malformed JSON, duplicate keys and invalid encodings require recovery."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            for payload in (b"", b"{broken", b"\xff", b'{"version":1,"version":1}', b"[]"):
                self.path().write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(RecoveryRequired):
                    load_journals(lock)
                with self.assertRaises(RecoveryRequired):
                    begin_batch(lock, replace(self.plan, plan_id="f" * 32))
                self.assertEqual(self.path().read_bytes(), payload)

    def test_strict_schema_rejects_unsafe_and_inconsistent_loaded_records(self):
        """Reject extra fields, types, paths, actions, identities and bad transitions."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            original = json.loads(self.path().read_text())
            edits = (
                lambda d: d.update(version=2),
                lambda d: d.update(approved=True),
                lambda d: d["plan"].update(root="relative"),
                lambda d: d["plan"].update(root=str(self.state)),
                lambda d: d["plan"].update(root_identity=[True, 1]),
                lambda d: d["plan"]["operations"][0]["request"].update(source="../escape"),
                lambda d: d["plan"]["operations"][0]["request"].update(destination="a/b/alpha.txt"),
                lambda d: d["plan"]["operations"][0]["request"].update(destination="Docs/other.txt"),
                lambda d: d["plan"]["operations"][0]["request"].update(destination="Trash/alpha.txt"),
                lambda d: d["plan"]["operations"][0]["request"].update(destination="Docs./alpha.txt"),
                lambda d: d["plan"]["operations"][0]["request"].update(action="trash"),
                lambda d: d["records"][0].update(status="done"),
                lambda d: d["records"][0].update(operation_id="0" * 32),
                lambda d: d["records"][0].update(intent_observations=[]),
                lambda d: d["records"][0]["intent_observations"][1].update(file_id=True),
                lambda d: d["records"][0].update(outcome_observations=[dict(
                    d["records"][0]["intent_observations"][1], relative_path="unrelated.txt")]),
                lambda d: d["plan"]["operations"][0]["observations"][1].update(relative_path="CON"),
                lambda d: d["records"].append(d["records"][0]),
            )
            for edit in edits:
                data = json.loads(json.dumps(original))
                edit(data)
                self.path().write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(edit=edit), self.assertRaises(RecoveryRequired):
                    load_journals(lock)

    def test_oversized_record_is_rejected_before_unbounded_read(self):
        """A journal exceeding the documented byte bound cannot be loaded."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            with patch("file_assistant.recovery.MAX_JOURNAL_BYTES", 100):
                with self.assertRaises(RecoveryRequired):
                    load_journals(lock)

    def test_case_variant_or_misnamed_journal_is_not_silently_ignored(self):
        """Unexpected journal names remain evidence and block new execution."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            renamed = self.state / ("JOURNAL-" + "a" * 32 + ".json")
            self.path().rename(renamed)
            with self.assertRaises(RecoveryRequired):
                load_journals(lock)
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, replace(self.plan, plan_id="b" * 32))

    def test_inaccessible_or_missing_journal_is_an_explicit_error(self):
        """A failed read after enumeration cannot masquerade as no saved intent."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            for error in (PermissionError("denied"), FileNotFoundError("disappeared")):
                with self.subTest(error=error):
                    with patch.object(Path, "open", side_effect=error):
                        with self.assertRaises(RecoveryRequired):
                            load_journals(lock)

    def test_supplied_intent_cannot_change_the_recorded_source_identity(self):
        """Fresh intent observations may not substitute a different source object."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            before = self.path().read_bytes()
            observations = tuple(replace(item, file_id=item.file_id + 1)
                                 if item.relative_path == "alpha.txt" else item
                                 for item in self.operation.observations)
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, self.operation.plan_id,
                              observations=observations)
            self.assertEqual(self.path().read_bytes(), before)

    def test_redirected_or_hardlinked_app_state_is_rejected(self):
        """Journal, lock and state redirects/hardlinks must not redirect app writes."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            for target in (self.state, self.path(), self.state / "execution.lock"):
                with self.subTest(target=target):
                    with patch.object(Path, "is_junction", new=lambda p: p == target):
                        with self.assertRaises(RecoveryRequired):
                            load_journals(lock)
            alias = self.area / "journal-alias"
            alias.hardlink_to(self.path())
            with self.assertRaises(RecoveryRequired):
                load_journals(lock)

    def test_invalid_outcome_or_out_of_order_intent_cannot_erase_evidence(self):
        """Only a pending operation may gain intent; success needs identity evidence."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            before = self.path().read_bytes()
            with self.assertRaises(RecoveryRequired):
                record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "success")
            self.assertEqual(self.path().read_bytes(), before)
            record_intent(lock, self.plan.plan_id, self.operation.plan_id)
            before = self.path().read_bytes()
            for status in ("success", "approved", None):
                with self.subTest(status=status), self.assertRaises(RecoveryRequired):
                    record_outcome(lock, self.plan.plan_id, self.operation.plan_id, status)
            self.assertEqual(self.path().read_bytes(), before)

    def test_mkdir_is_recorded_and_move_requires_created_parent_evidence(self):
        """Represent both actions and retain the newly observed parent identity."""
        self.plan = self.make_plan("NewFolder")
        mkdir, move = self.plan.operations
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, move.plan_id)
            record_intent(lock, self.plan.plan_id, mkdir.plan_id)
            (self.root / "NewFolder").mkdir()  # Simulated executor effect, fixture only.
            parent = self.observed("NewFolder")
            record_outcome(lock, self.plan.plan_id, mkdir.plan_id, "success",
                           observations=(parent,))
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, move.plan_id)
            observations = tuple(
                parent if item.relative_path == "NewFolder" else
                replace(item, parent_device=parent.device, parent_file_id=parent.file_id)
                if item.relative_path == "NewFolder/alpha.txt" else item
                for item in move.observations
            )
            wrong_observations = tuple(replace(item, file_id=item.file_id + 1)
                                       if item.relative_path == "NewFolder" else
                                       replace(item, parent_file_id=item.parent_file_id + 1)
                                       if item.relative_path == "NewFolder/alpha.txt" else item
                                       for item in observations)
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, move.plan_id,
                              observations=wrong_observations)
            record_intent(lock, self.plan.plan_id, move.plan_id, observations=observations)
            self.assertEqual(load_journals(lock)[0].records[-1].intent_observations, observations)


class RecoveryInspectionTests(RecoveryFixtures):
    """Recovery reports evidence without retrying or changing managed content."""

    def test_source_only_and_destination_only_match_recorded_identity(self):
        """Distinguish the original object at its source versus its destination."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            self.assertEqual(self.finding(lock).conclusion, "source_only")
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
            self.assertEqual(self.finding(lock).conclusion, "destination_only")

    def test_both_present_and_neither_present_remain_uncertain(self):
        """Neither ambiguous state grants authority to repair or retry."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            destination = self.root / "Documents/alpha.txt"
            destination.touch()
            self.assertEqual(self.finding(lock).conclusion, "both_present")
            destination.unlink()
            (self.root / "alpha.txt").unlink()
            self.assertEqual(self.finding(lock).conclusion, "neither_present")

    def test_same_name_size_and_mtime_do_not_hide_identity_replacement(self):
        """An unrelated destination is not a completed move even with matching metadata."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            previous = (self.root / "alpha.txt").stat()
            (self.root / "alpha.txt").rename(self.area / "previous.txt")
            destination = self.root / "Documents/alpha.txt"
            destination.write_bytes(b"synthetic fixture")
            os.utime(destination, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertEqual(self.finding(lock).conclusion, "identity_mismatch")

    def test_changed_content_metadata_or_link_count_is_uncertain(self):
        """Matching identity alone does not excuse modified size or timestamps."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            source = self.root / "alpha.txt"
            source.write_bytes(b"changed synthetic fixture")
            self.assertEqual(self.finding(lock).conclusion, "identity_mismatch")

    def test_new_hard_link_is_an_identity_metadata_mismatch(self):
        """Recovery refuses to infer success for an object that gained a hard link."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.area / "alias.txt").hardlink_to(self.root / "alpha.txt")
            self.assertEqual(self.finding(lock).conclusion, "identity_mismatch")

    def test_missing_or_replaced_root_is_uncertain_not_no_journal(self):
        """A missing/replaced root cannot erase an unfinished execution record."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            self.root.rename(self.area / "old-root")
            self.assertEqual(self.finding(lock).conclusion, "uncertain")
            self.root.mkdir()
            self.assertEqual(self.finding(lock).conclusion, "uncertain")
            self.assertEqual(len(load_journals(lock)), 1)

    def test_replaced_destination_parent_is_uncertain(self):
        """Even an absent destination must have the recorded safe parent identity."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            folder = self.root / "Documents"
            folder.rename(self.area / "old-Documents")
            folder.mkdir()
            self.assertEqual(self.finding(lock).conclusion, "uncertain")

    def test_redirects_and_permission_errors_produce_uncertainty(self):
        """Unsafe or inaccessible paths are reported without following them."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            for target in (self.root, self.root / "alpha.txt", self.root / "Documents"):
                with self.subTest(target=target):
                    with patch.object(Path, "is_junction", new=lambda p: p == target):
                        self.assertEqual(self.finding(lock).conclusion, "uncertain")
            with patch.object(Path, "is_junction", new=lambda p: p == self.area):
                with self.assertRaises(RecoveryRequired):
                    inspect_recovery(lock)  # The same ancestor also protects app state.
            original = Path.lstat

            def denied(path, *args, **kwargs):
                if path == self.root / "alpha.txt":
                    raise PermissionError("synthetic denied")
                return original(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=denied):
                self.assertEqual(self.finding(lock).conclusion, "uncertain")

    def test_interrupted_mkdir_does_not_claim_ownership_of_an_appeared_directory(self):
        """An intent alone cannot identify which process created a new folder."""
        self.plan = self.make_plan("NewFolder")
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            record_intent(lock, self.plan.plan_id, self.plan.operations[0].plan_id)
            self.assertEqual(self.finding(lock).conclusion, "directory_absent")
            (self.root / "NewFolder").mkdir()
            self.assertEqual(self.finding(lock).conclusion, "uncertain")

    def test_inspection_never_opens_contents_or_mutates_managed_files_or_journal(self):
        """Only app-owned JSON reads and managed metadata inspection are permitted."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            before = self.path().read_bytes()
            original_open = io.open

            def guarded_open(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path).is_relative_to(self.root):
                    raise AssertionError("managed content read")
                return original_open(path, *args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch("io.open", side_effect=guarded_open))
                stack.enter_context(patch("builtins.open", side_effect=guarded_open))
                for owner, names in ((os, ("mkdir", "rmdir", "rename", "replace", "remove", "unlink")),
                                     (Path, ("mkdir", "rename", "replace", "unlink", "rmdir"))):
                    for name in names:
                        stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
                self.assertEqual(self.finding(lock).conclusion, "source_only")
            self.assertEqual(self.path().read_bytes(), before)
            self.assertEqual((self.root / "alpha.txt").read_bytes(), b"synthetic fixture")
            self.assertEqual(list((self.root / "Documents").iterdir()), [])


class JournalCompletionTests(RecoveryFixtures):
    """Terminal stops retain evidence while uncertainty needs explicit resolution."""

    def test_pending_remainder_can_finish_without_replaying_its_plan(self):
        """A live executor can close known unsubmitted work and permit a new batch."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
            finished = finish_unexecuted(lock, self.plan.plan_id, message="Cancelled before execution.")
            self.assertEqual(finished.records[0].status, "not_executed")
            self.assertTrue(is_complete(finished))
            self.assertEqual(inspect_recovery(lock), ())
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, self.plan)
            begin_batch(lock, replace(self.plan, plan_id="a" * 32))

    def test_unsubmitted_intent_is_retained_when_marked_not_executed(self):
        """A known unsubmitted intent can close without erasing its observations."""
        with ExecutionLock(self.root, self.state) as lock:
            original = self.intent(lock)
            finished = finish_unexecuted(lock, self.plan.plan_id, message="Cancelled before native call.")
            self.assertEqual(finished.records[0].status, "not_executed")
            self.assertEqual(finished.records[0].intent_observations, original.records[0].intent_observations)
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, self.operation.plan_id)

    def test_attempted_or_failed_outcome_intent_is_never_automatically_closed(self):
        """Submission is recorded before mutation; close cannot relabel uncertainty."""
        with ExecutionLock(self.root, self.state) as lock:
            original = self.intent(lock)
            mark_attempted(lock, self.plan.plan_id, self.operation.plan_id)
            retained = finish_unexecuted(lock, self.plan.plan_id, message="Stop after attempt.")
            self.assertFalse(is_complete(retained))
            self.assertEqual(retained.records, original.records)
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, replace(self.plan, plan_id="a" * 32))

    def test_restart_cannot_claim_an_old_intent_was_never_submitted(self):
        """A new process lacks the original executor's knowledge of nonexecution."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
        with ExecutionLock(self.root, self.state) as lock:
            before = self.path().read_bytes()
            with self.assertRaises(RecoveryRequired):
                finish_unexecuted(lock, self.plan.plan_id, message="Cannot infer nonexecution.")
            self.assertEqual(self.path().read_bytes(), before)

    def test_completion_write_failure_leaves_prior_evidence_blocking(self):
        """Failed terminal-state persistence cannot make the batch complete."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            before = self.path().read_bytes()
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(PersistenceError):
                    finish_unexecuted(lock, self.plan.plan_id, message="Cancelled.")
            self.assertEqual(self.path().read_bytes(), before)
            self.assertFalse(is_complete(load_journals(lock)[0]))

    def test_explicit_resolution_preserves_original_intent_and_reviewed_evidence(self):
        """Acknowledge an observed move without rewriting its uncertain historical record."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.root / "alpha.txt").rename(self.root / "Documents/alpha.txt")
        with ExecutionLock(self.root, self.state) as lock:
            original = load_journals(lock)[0]
            findings = inspect_recovery(lock)
            resolved = resolve_recovery(lock, original, findings, confirmed=True,
                                        message="User reviewed original identity at destination.")
            self.assertEqual(resolved.plan, original.plan)
            self.assertEqual(resolved.records, original.records)
            self.assertEqual(resolved.resolution.findings, findings)
            self.assertTrue(is_complete(resolved))
            self.assertEqual(inspect_recovery(lock), ())
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, self.plan)
            with self.assertRaises(RecoveryRequired):
                record_intent(lock, self.plan.plan_id, self.operation.plan_id)
            self.assertEqual(load_journals(lock)[0], resolved)
            begin_batch(lock, replace(self.plan, plan_id="a" * 32, operations=()))

    def test_resolution_requires_confirmation_and_reason(self):
        """Truthy values and empty notes do not constitute explicit recovery resolution."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            before = self.path().read_bytes()
            for confirmed, message in ((False, "Reviewed"), (1, "Reviewed"), (True, ""), (True, "  ")):
                with self.subTest(confirmed=confirmed), self.assertRaises(RecoveryRequired):
                    resolve_recovery(lock, journal, findings, confirmed=confirmed, message=message)
            self.assertEqual(self.path().read_bytes(), before)

    def test_stale_review_cannot_resolve_changed_file_observations(self):
        """Actual metadata, not just a source-only/destination-only label, binds review."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.root / "alpha.txt").write_bytes(b"first unexpected change")
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            self.assertEqual(findings[0].conclusion, "identity_mismatch")
            self.assertTrue(findings[0].observations)
            (self.root / "alpha.txt").write_bytes(b"changed after user review")
            self.assertEqual(inspect_recovery(lock)[0].conclusion, "identity_mismatch")
            before = self.path().read_bytes()
            with self.assertRaises(RecoveryRequired):
                resolve_recovery(lock, journal, findings, confirmed=True, message="Stale review.")
            self.assertEqual(self.path().read_bytes(), before)

    def test_changed_journal_or_partial_findings_cannot_be_resolved(self):
        """The exact historical record and full unfinished batch must be reviewed."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            with self.assertRaises(RecoveryRequired):
                resolve_recovery(lock, journal, (), confirmed=True, message="Incomplete review.")
            record_outcome(lock, self.plan.plan_id, self.operation.plan_id, "failed", message="Outcome changed.")
            with self.assertRaises(RecoveryRequired):
                resolve_recovery(lock, journal, findings, confirmed=True, message="Stale journal.")

    def test_resolution_write_failure_keeps_uncertainty_and_blocks_new_batches(self):
        """Resolution becomes effective only after its own durable publication."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            before = self.path().read_bytes()
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(PersistenceError):
                    resolve_recovery(lock, journal, findings, confirmed=True, message="Reviewed source-only state.")
            self.assertEqual(self.path().read_bytes(), before)
        with ExecutionLock(self.root, self.state) as lock:
            with self.assertRaises(RecoveryRequired):
                begin_batch(lock, replace(self.plan, plan_id="a" * 32))

    def test_resolution_does_not_infer_success_from_ambiguous_both_present(self):
        """Explicit acknowledgement retains both-present uncertainty and all files."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            (self.root / "Documents/alpha.txt").write_bytes(b"other fixture")
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            resolved = resolve_recovery(lock, journal, findings, confirmed=True,
                                        message="User inspected both files; preserve both and close this batch.")
            self.assertEqual(resolved.records[0].status, "intent")
            self.assertEqual(resolved.resolution.findings[0].conclusion, "both_present")
            self.assertEqual((self.root / "alpha.txt").read_bytes(), b"synthetic fixture")
            self.assertEqual((self.root / "Documents/alpha.txt").read_bytes(), b"other fixture")

    def test_unsafe_or_inaccessible_paths_cannot_be_acknowledged_as_safe_resolution(self):
        """A redirect or failed inspection remains blocking evidence."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal = load_journals(lock)[0]
            with patch.object(Path, "is_junction", new=lambda path: path == self.root / "Documents"):
                findings = inspect_recovery(lock)
                with self.assertRaises(RecoveryRequired):
                    resolve_recovery(lock, journal, findings, confirmed=True, message="Unsafe path.")

    def test_resolution_never_reads_or_changes_managed_files(self):
        """Acknowledgement writes only the app-owned journal and preserves all user bytes."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            original_open = io.open

            def guarded(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path).is_relative_to(self.root):
                    raise AssertionError("managed file content access")
                return original_open(path, *args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch("io.open", side_effect=guarded))
                stack.enter_context(patch("builtins.open", side_effect=guarded))
                for name in ("mkdir", "rename", "replace", "unlink", "rmdir", "write_bytes", "write_text"):
                    stack.enter_context(patch.object(Path, name, side_effect=AssertionError(name)))
                resolve_recovery(lock, journal, findings, confirmed=True,
                                  message="User reviewed unchanged source and absent destination.")
            self.assertEqual((self.root / "alpha.txt").read_bytes(), b"synthetic fixture")
            self.assertEqual(list((self.root / "Documents").iterdir()), [])

    def test_malformed_completion_and_resolution_records_fail_closed(self):
        """Corrupt terminal annotations cannot silently release an unresolved batch."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            resolved = resolve_recovery(lock, journal, findings, confirmed=True,
                                        message="Reviewed source-only state.")
            valid = self.path().read_bytes()
            edits = (
                lambda d: d["resolution"].update(message=""),
                lambda d: d["resolution"].update(findings=[]),
                lambda d: d["resolution"].update(approved=True),
                lambda d: d["resolution"]["findings"][0].update(operation_id="a" * 32),
                lambda d: d["resolution"]["findings"][0].update(conclusion="not_started"),
                lambda d: d["resolution"]["findings"][0].update(observations=[]),
                lambda d: d["resolution"]["findings"][0]["observations"][0].update(relative_path="../outside"),
                lambda d: d["records"][0].update(status="not_executed", message=""),
            )
            for edit in edits:
                data = json.loads(valid)
                edit(data)
                self.path().write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(edit=edit), self.assertRaises(RecoveryRequired):
                    load_journals(lock)
            self.path().write_bytes(valid)
            self.assertEqual(load_journals(lock)[0], resolved)

    def test_existing_unresolved_cancellation_record_can_be_explicitly_closed(self):
        """A previous session's pending-only journal has a supported retained resolution."""
        with ExecutionLock(self.root, self.state) as lock:
            begin_batch(lock, self.plan)
        with ExecutionLock(self.root, self.state) as lock:
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            resolved = resolve_recovery(lock, journal, findings, confirmed=True,
                                        message="User reviewed old cancellation with no submitted operations.")
            self.assertEqual(resolved.records[0].status, "pending")
            self.assertTrue(is_complete(resolved))
            begin_batch(lock, replace(self.plan, plan_id="a" * 32))

    def test_missing_root_resolution_records_absence_without_claiming_success(self):
        """Explicit review can acknowledge a missing historical root while retaining intent."""
        with ExecutionLock(self.root, self.state) as lock:
            self.intent(lock)
        self.root.rename(self.area / "previous-workspace")
        with ExecutionLock(self.root, self.state) as lock:
            journal, findings = load_journals(lock)[0], inspect_recovery(lock)
            resolved = resolve_recovery(lock, journal, findings, confirmed=True,
                                        message="User verified root was relocated outside this batch.")
            self.assertEqual(resolved.records[0].status, "intent")
            self.assertEqual(resolved.resolution.findings[0].observations, (PathObservation(".", False),))


class ExecutionLockTests(RecoveryFixtures):
    """A single shared state directory coordinates all application instances."""

    def test_competing_process_is_blocked_even_for_a_different_root(self):
        """The lock covers the app, not merely a particular organization root."""
        other = self.area / "other-root"
        other.mkdir()
        with ExecutionLock(self.root, self.state):
            process, connection, result = self.start_process(other)
            self.assertEqual(result, "busy")
            process.join(10)
            self.assertEqual(process.exitcode, 0)

    def test_normal_release_allows_another_process_and_keeps_lock_file(self):
        """A persistent lock file is harmless after its byte-range lock is released."""
        with ExecutionLock(self.root, self.state):
            pass
        self.assertTrue((self.state / "execution.lock").exists())
        process, connection, result = self.start_process()
        self.assertEqual(result, "locked")
        connection.send("exit")
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_process_exit_releases_lock_without_context_cleanup(self):
        """Windows releases the lock after abrupt owner exit, without deleting a file."""
        process, connection, result = self.start_process(abrupt=True)
        self.assertEqual(result, "locked")
        with self.assertRaises(LockUnavailable):
            with ExecutionLock(self.root, self.state):
                self.fail("contending lock accepted")
        connection.send("exit")
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        with ExecutionLock(self.root, self.state) as lock:
            self.assertEqual(load_journals(lock), ())


if __name__ == "__main__":
    unittest.main()
