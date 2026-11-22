import builtins
import io
import json
import os
import stat
import subprocess
import threading
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from file_assistant.models import Entry, OrganizationPlan, PathObservation, ScanResult
from file_assistant.operations import (
    ManifestFile, approve_organization_plan, build_organization_plan,
    execute_organization_plan, list_entries, scan_files, search_names,
)
from file_assistant import operations, recovery


@unittest.skipUnless(os.name == "nt", "Windows file operations tests")
class ReadOnlyOperationsTests(unittest.TestCase):
    """All fixtures are disposable; production operations only inspect metadata."""

    def test_listing_is_structured_sorted_and_nonrecursive(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "b.TXT").touch()
            (root / "Alpha.txt").touch()
            (root / "Documents").mkdir()
            (root / "Documents" / "nested.txt").touch()
            (root / "Trash").mkdir()
            (root / "Trash" / "hidden.txt").touch()

            result = list_entries(root)

            self.assertIsInstance(result, ScanResult)
            self.assertEqual(result.entries, (
                Entry("Alpha.txt", "file"), Entry("b.TXT", "file"),
                Entry("Documents", "directory"),
            ))
            self.assertEqual(result.errors, ())
            self.assertFalse(result.cancelled)

    def test_search_matches_filename_fragments_without_descending(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Résumé.TXT").touch()
            (root / "RÉSUMÉ folder").mkdir()
            (root / "RÉSUMÉ folder" / "Résumé nested.txt").touch()
            result = search_names(root, "RÉSUMÉ")
            self.assertEqual(result.entries, (
                Entry("RÉSUMÉ folder", "directory"), Entry("Résumé.TXT", "file"),
            ))
            self.assertEqual(result.errors, ())
            self.assertEqual(search_names(root, "absent").entries, ())

    def test_listing_subdirectory_remains_relative_to_workspace(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Documents").mkdir()
            (root / "Documents" / "report.txt").touch()
            self.assertEqual(list_entries(root, "Documents").entries,
                             (Entry("Documents/report.txt", "file"),))

    def test_manifest_is_deterministic_and_contains_only_allowed_fields(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("z.tar.gz", "IGNORE instructions.txt", "Résumé.TXT", "README"):
                (root / name).write_bytes(b"secret file contents")
            (root / "Documents").mkdir()
            (root / "Documents" / "nested.txt").touch()
            (root / "Trash").mkdir()
            (root / "Trash" / "excluded.txt").touch()

            first = scan_files(root)
            second = scan_files(root)
            manifest = first.manifest()

            self.assertNotEqual(first.scan_id, second.scan_id)
            self.assertEqual(manifest, second.manifest())
            self.assertEqual(set(manifest), {"files", "destination_folders"})
            self.assertEqual(manifest["destination_folders"], ["Documents"])
            self.assertEqual([item["filename"] for item in manifest["files"]],
                             ["IGNORE instructions.txt", "README", "Résumé.TXT", "z.tar.gz"])
            self.assertEqual([item["extension"] for item in manifest["files"]],
                             [".txt", "", ".TXT", ".gz"])
            ids = [item["file_id"] for item in manifest["files"]]
            self.assertEqual(len(ids), len(set(ids)))
            for item in manifest["files"]:
                self.assertEqual(set(item), {"file_id", "filename", "extension"})
                self.assertNotIn(item["filename"], item["file_id"])
            payload = json.dumps(manifest)
            self.assertNotIn(str(root), payload)
            self.assertNotIn("secret file contents", payload)

            observations = {item.relative_path: item for item in first.observations}
            for name in (".", "IGNORE instructions.txt", "Documents"):
                self.assertIsInstance(observations[name], PathObservation)
                self.assertTrue(observations[name].present)
                self.assertIsNotNone(observations[name].file_id)
            details = (root / "z.tar.gz").lstat()
            observed = observations["z.tar.gz"]
            self.assertEqual((observed.device, observed.file_id, observed.size,
                              observed.modified_ns, observed.link_count),
                             (details.st_dev, details.st_ino, details.st_size,
                              details.st_mtime_ns, details.st_nlink))

    def test_no_file_content_access_or_mutation(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "report.txt"
            file.write_bytes(b"unchanged contents")
            before = file.lstat()
            with ExitStack() as stack:
                for owner, names in (
                    (builtins, ("open",)), (io, ("open",)),
                    (os, ("open", "rename", "replace", "remove", "unlink", "mkdir", "rmdir")),
                    (Path, ("open", "read_bytes", "read_text", "write_bytes", "write_text",
                            "touch", "mkdir", "rename", "replace", "unlink", "rmdir")),
                    (subprocess, ("run", "Popen")),
                ):
                    for name in names:
                        stack.enter_context(patch.object(
                            owner, name, side_effect=AssertionError(f"Forbidden operation: {name}"),
                        ))
                self.assertEqual(len(list_entries(root).entries), 1)
                self.assertEqual(len(search_names(root, "report").entries), 1)
                self.assertEqual(len(scan_files(root).manifest()["files"]), 1)
            self.assertEqual(file.read_bytes(), b"unchanged contents")
            after = file.lstat()
            self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                             (after.st_ino, after.st_size, after.st_mtime_ns))

    def test_invalid_roots_and_paths_report_errors_without_widening_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "file.txt").touch()
            for workspace, user_path in (
                (root / "missing", "."), (root / "file.txt", "."),
                (Path("relative"), "."), (root, ".."), (root, "Trash"),
                (root, "C:/Windows"), (root, "file.txt"),
            ):
                with self.subTest(workspace=workspace, user_path=user_path):
                    result = list_entries(workspace, user_path)
                    self.assertEqual(result.entries, ())
                    self.assertTrue(result.errors)

    def test_inaccessible_entry_is_reported_and_manifest_is_blocked(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blocked = root / "blocked.txt"
            blocked.touch()
            (root / "safe.txt").touch()
            original_lstat = Path.lstat

            def inaccessible(path, *args, **kwargs):
                if path == blocked:
                    raise PermissionError("Access denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", new=inaccessible):
                snapshot = scan_files(root)
                self.assertEqual(snapshot.result.entries, (Entry("safe.txt", "file"),))
                self.assertTrue(snapshot.result.errors)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    snapshot.manifest()
                result = search_names(root, "safe")
                self.assertTrue(result.errors)

    def test_inaccessible_directory_is_an_error_not_an_empty_success(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty = scan_files(root)
            self.assertEqual(empty.manifest()["files"], [])
            with patch("file_assistant.operations.os.scandir", side_effect=PermissionError("denied")):
                snapshot = scan_files(root)
                self.assertEqual(snapshot.result.entries, ())
                self.assertTrue(snapshot.result.errors)
                with self.assertRaises(ValueError):
                    snapshot.manifest()

    def test_mid_iteration_failure_preserves_partial_results_and_error(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "report.txt"
            file.touch()

            class InterruptedDirectory:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def __iter__(self):
                    yield SimpleNamespace(name=file.name)
                    raise PermissionError("Directory enumeration interrupted")

            with patch("file_assistant.operations.os.scandir", return_value=InterruptedDirectory()):
                result = list_entries(root)
            self.assertEqual(result.entries, (Entry("report.txt", "file"),))
            self.assertTrue(result.errors)

    def test_redirects_and_unsupported_objects_are_excluded_and_reported(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            redirected = root / "redirected"
            redirected.mkdir()
            original_lstat = Path.lstat
            for mode, attributes in (
                (stat.S_IFLNK, 0), (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT),
                (stat.S_IFIFO, 0),
            ):
                with self.subTest(mode=mode, attributes=attributes):
                    def simulated_lstat(path, *args, **kwargs):
                        if path == redirected:
                            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                        return original_lstat(path, *args, **kwargs)

                    with patch.object(Path, "lstat", new=simulated_lstat):
                        result = list_entries(root)
                    self.assertEqual(result.entries, ())
                    self.assertTrue(result.errors)

    def test_real_junction_is_never_enumerated(self):
        with TemporaryDirectory() as temp_dir:
            area = Path(temp_dir)
            root = area / "workspace"
            root.mkdir()
            target = area / "outside"
            target.mkdir()
            (target / "outside.txt").touch()
            junction = root / "redirected"
            environment = os.environ.copy()
            environment["FILE_ASSISTANT_TEST_JUNCTION"] = str(junction)
            environment["FILE_ASSISTANT_TEST_TARGET"] = str(target)
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "New-Item -ItemType Junction -Path $env:FILE_ASSISTANT_TEST_JUNCTION "
                     "-Target $env:FILE_ASSISTANT_TEST_TARGET -ErrorAction Stop | Out-Null"],
                    env=environment, check=True, capture_output=True, text=True,
                )
                self.assertTrue(junction.is_junction())
                original_scandir = os.scandir
                visited = []

                def record_scandir(path):
                    visited.append(Path(path))
                    return original_scandir(path)

                with patch("file_assistant.operations.os.scandir", side_effect=record_scandir):
                    snapshot = scan_files(root)
                    refused_root = list_entries(junction)
                self.assertEqual(visited, [root.resolve(strict=True)])
                self.assertEqual(snapshot.result.entries, ())
                self.assertTrue(snapshot.result.errors)
                self.assertTrue(refused_root.errors)
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()

    def test_real_symlink_is_excluded_without_accessing_target(self):
        with TemporaryDirectory() as temp_dir:
            area = Path(temp_dir)
            root = area / "workspace"
            root.mkdir()
            target = area / "outside.txt"
            target.touch()
            link = root / "linked.txt"
            try:
                link.symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows has not granted symbolic-link creation permission")
                raise
            snapshot = scan_files(root)
            self.assertEqual(snapshot.result.entries, ())
            self.assertTrue(snapshot.result.errors)

    def test_changed_canonical_parent_does_not_expand_scan_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            file = root / "file.txt"
            file.touch()
            nested = root / "nested"
            nested.mkdir()
            original_resolve = Path.resolve

            def changed_parent(path, *args, **kwargs):
                if path == file:
                    return nested / "file.txt"
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", new=changed_parent):
                result = list_entries(root)
            self.assertEqual(result.entries, (Entry("nested", "directory"),))
            self.assertTrue(any("immediate child" in error for error in result.errors))

    def test_entry_limit_is_explicit_and_prevents_manifest_use(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "first.txt").touch()
            (root / "second.txt").touch()
            self.assertFalse(scan_files(root, max_entries=2).result.errors)
            snapshot = scan_files(root, max_entries=1)
            self.assertTrue(any("limit" in error.lower() for error in snapshot.result.errors))
            with self.assertRaises(ValueError):
                snapshot.manifest()

    def test_manifest_byte_limit_is_checked_on_serialized_payload(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Résumé.txt").touch()
            size = len(json.dumps(scan_files(root).manifest(), ensure_ascii=True).encode("utf-8"))
            self.assertFalse(scan_files(root, max_manifest_bytes=size).result.errors)
            snapshot = scan_files(root, max_manifest_bytes=size - 1)
            self.assertTrue(any("limit" in error.lower() for error in snapshot.result.errors))
            with self.assertRaises(ValueError):
                snapshot.manifest()

    def test_multiply_linked_files_are_not_proposed_for_mutation(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.txt"
            source.touch()
            (root / "alias.txt").hardlink_to(source)
            snapshot = scan_files(root)
            self.assertEqual(snapshot.result.entries, ())
            self.assertTrue(snapshot.result.errors)
            with self.assertRaises(ValueError):
                snapshot.manifest()


@unittest.skipUnless(os.name == "nt", "Windows review planning tests")
class PlanningFixtures(unittest.TestCase):
    """Shared disposable root and synthetic proposals; never use real user files."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        self.root = self.area / "workspace"
        self.root.mkdir()
        (self.root / "alpha.txt").write_bytes(b"fixture alpha")
        (self.root / "beta.jpg").write_bytes(b"fixture beta")

    def snapshot(self):
        snapshot = scan_files(self.root)
        self.assertFalse(snapshot.result.errors)
        return snapshot

    def proposal(self, snapshot, destinations=None):
        destinations = destinations or {"alpha.txt": "Documents", "beta.jpg": "Pictures"}
        return {"moves": [{"file_id": item.file_id,
                           "destination_folder": destinations[item.filename]}
                          for item in snapshot.files if item.filename in destinations]}

    def build(self, snapshot, proposal, **kwargs):
        return build_organization_plan(
            self.root, snapshot, proposal, proposal_scan_id=snapshot.scan_id, **kwargs,
        )


class ReviewPlanTests(PlanningFixtures):
    """Review data preserves names and derives only selected folder creations."""

    def test_new_folders_and_moves_are_immutable_review_data(self):
        """Bind child creations and unchanged basenames to the original root/scan."""
        snapshot = self.snapshot()
        batch = self.build(snapshot, self.proposal(snapshot))
        self.assertIsInstance(batch, OrganizationPlan)
        self.assertEqual((batch.root, batch.scan_id), (snapshot.root, snapshot.scan_id))
        root_obs = next(item for item in snapshot.observations if item.relative_path == ".")
        self.assertEqual(batch.root_identity, (root_obs.device, root_obs.file_id))
        self.assertEqual([(op.request.action, op.request.source, op.request.destination)
                          for op in batch.operations], [
            ("mkdir", None, "Documents"), ("mkdir", None, "Pictures"),
            ("move", "alpha.txt", "Documents/alpha.txt"),
            ("move", "beta.jpg", "Pictures/beta.jpg"),
        ])
        ids = [batch.plan_id] + [op.plan_id for op in batch.operations]
        self.assertEqual(len(ids), len(set(ids)))
        with self.assertRaises(FrozenInstanceError):
            batch.plan_id = "changed"
        with self.assertRaises(FrozenInstanceError):
            batch.operations[-1].request.destination = "other"
        for op in batch.operations:
            self.assertEqual((op.root, op.root_identity), (batch.root, batch.root_identity))
            self.assertFalse(hasattr(op, "approved"))
        self.assertFalse(hasattr(batch, "approved"))

    def test_existing_folder_uses_canonical_spelling_and_parent_identity(self):
        """Reuse a scanned safe folder without a redundant mkdir request."""
        folder = self.root / "Documents"
        folder.mkdir()
        snapshot = self.snapshot()
        batch = self.build(snapshot, self.proposal(snapshot, {"alpha.txt": "documents"}))
        self.assertEqual(len(batch.operations), 1)
        operation = batch.operations[0]
        self.assertEqual(operation.request.destination, "Documents/alpha.txt")
        destination = next(item for item in operation.observations
                           if item.relative_path == "Documents/alpha.txt")
        self.assertFalse(destination.present)
        details = folder.lstat()
        self.assertEqual((destination.parent_device, destination.parent_file_id),
                         (details.st_dev, details.st_ino))

    def test_new_folder_observations_do_not_invent_parent_identity(self):
        """Record the absent folder and child; T9 must observe the created parent."""
        snapshot = self.snapshot()
        batch = self.build(snapshot, self.proposal(snapshot, {"alpha.txt": "Documents"}))
        move = batch.operations[-1]
        observed = {item.relative_path: item for item in move.observations}
        self.assertFalse(observed["Documents"].present)
        self.assertEqual(observed["Documents"].parent_file_id, batch.root_identity[1])
        self.assertFalse(observed["Documents/alpha.txt"].present)
        self.assertIsNone(observed["Documents/alpha.txt"].parent_file_id)

    def test_shared_case_variant_folder_is_created_only_once(self):
        """Normalize folder case variants to one reviewed creation and spelling."""
        snapshot = self.snapshot()
        batch = self.build(snapshot, self.proposal(
            snapshot, {"alpha.txt": "Docs", "beta.jpg": "docs"},
        ))
        self.assertEqual([op.request.destination for op in batch.operations],
                         ["Docs", "Docs/alpha.txt", "Docs/beta.jpg"])

    def test_deselection_removes_unused_creations_and_empty_selection_is_empty(self):
        """A selected subset contains only its moves and necessary new folders."""
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot)
        selected = (snapshot.files[0].file_id,)
        subset = self.build(snapshot, proposal, selected_file_ids=selected)
        self.assertEqual([op.request.destination for op in subset.operations],
                         ["Documents", "Documents/alpha.txt"])
        empty = self.build(snapshot, proposal, selected_file_ids=())
        self.assertEqual(empty.operations, ())
        self.assertEqual(empty.scan_id, snapshot.scan_id)

    def test_fresh_identity_and_no_mutable_proposal_aliases(self):
        """Changing a proposal or selection creates fresh review identities."""
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot)
        first = self.build(snapshot, proposal)
        subset = self.build(snapshot, proposal, selected_file_ids=(snapshot.files[0].file_id,))
        proposal["moves"][0]["destination_folder"] = "Texts"
        changed = self.build(snapshot, proposal)
        self.assertEqual(len({first.plan_id, subset.plan_id, changed.plan_id}), 3)
        self.assertEqual(first.operations[-2].request.destination, "Documents/alpha.txt")
        self.assertEqual(changed.operations[-2].request.destination, "Texts/alpha.txt")
        self.assertTrue(set(op.plan_id for op in first.operations).isdisjoint(
            op.plan_id for op in subset.operations))

    def test_unicode_basename_is_preserved(self):
        """Move targets retain the exact scanned Unicode filename and extension."""
        (self.root / "Résumé.TXT").touch()
        snapshot = self.snapshot()
        batch = self.build(snapshot, self.proposal(snapshot, {"Résumé.TXT": "Notes"}))
        self.assertEqual(batch.operations[-1].request.source, "Résumé.TXT")
        self.assertEqual(batch.operations[-1].request.destination, "Notes/Résumé.TXT")

    def test_complete_empty_scan_and_empty_proposal_are_valid(self):
        """An empty complete scan yields empty review data with trusted context."""
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = scan_files(root)
            batch = build_organization_plan(root, snapshot, '{"moves":[]}',
                                            proposal_scan_id=snapshot.scan_id)
            self.assertEqual(batch.operations, ())
            self.assertEqual(batch.scan_id, snapshot.scan_id)

    def test_no_mutation_content_access_network_or_shell_calls(self):
        """Planning validates metadata without performing any proposed operation."""
        snapshot = self.snapshot()
        before = {path.name: path.lstat() for path in self.root.iterdir()}
        with ExitStack() as stack:
            for owner, names in (
                (builtins, ("open",)), (io, ("open",)),
                (os, ("open", "mkdir", "rmdir", "rename", "replace", "remove", "unlink")),
                (Path, ("open", "read_bytes", "read_text", "write_bytes", "write_text",
                        "touch", "mkdir", "rename", "replace", "unlink", "rmdir")),
                (subprocess, ("run", "Popen")),
            ):
                for name in names:
                    stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name)))
            stack.enter_context(patch("file_assistant.ollama_client.HTTPConnection",
                                      side_effect=AssertionError("network")))
            self.build(snapshot, self.proposal(snapshot))
        self.assertEqual(set(before), {path.name for path in self.root.iterdir()})
        for name, details in before.items():
            after = (self.root / name).lstat()
            self.assertEqual((details.st_ino, details.st_size, details.st_mtime_ns),
                             (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual((self.root / "alpha.txt").read_bytes(), b"fixture alpha")


class PlanContextTests(PlanningFixtures):
    """Request context comes from trusted application code, never model fields."""

    def test_incomplete_cancelled_or_invalid_scan_is_rejected(self):
        """Neither partial results nor a fabricated non-snapshot input can plan."""
        snapshot = self.snapshot()
        for bad in (None, {}, replace(snapshot, root=None),
                    replace(snapshot, result=ScanResult((), ("denied",), False)),
                    replace(snapshot, result=ScanResult((), (), True))):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    build_organization_plan(self.root, bad, {"moves": []},
                                            proposal_scan_id=snapshot.scan_id)

    def test_delayed_response_cannot_reuse_ids_from_a_new_scan(self):
        """Recurring f1 IDs do not make an old response belong to a newer scan."""
        original = self.snapshot()
        newer = self.snapshot()
        self.assertEqual(original.files[0].file_id, newer.files[0].file_id)
        with self.assertRaisesRegex(ValueError, "scan"):
            build_organization_plan(self.root, newer, self.proposal(original),
                                    proposal_scan_id=original.scan_id)

    def test_request_context_is_required_and_cannot_be_ai_supplied(self):
        """Missing, malformed or model-controlled scan bindings are refused."""
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot)
        with self.assertRaises(TypeError):
            build_organization_plan(self.root, snapshot, proposal)
        for context in (None, "", True, "other-scan"):
            with self.subTest(context=context), self.assertRaises(ValueError):
                build_organization_plan(self.root, snapshot, proposal, proposal_scan_id=context)
        with self.assertRaises(ValueError):
            self.build(snapshot, dict(proposal, scan_id=snapshot.scan_id))

    def test_changed_current_workspace_rejects_old_root_context(self):
        """A response captured for one root cannot produce a plan in another."""
        snapshot = self.snapshot()
        other = self.area / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            build_organization_plan(other, snapshot, self.proposal(snapshot),
                                    proposal_scan_id=snapshot.scan_id)

    def test_missing_or_duplicate_observations_are_rejected(self):
        """Required scan evidence cannot be silently reconstructed or overwritten."""
        snapshot = self.snapshot()
        for observations in (snapshot.observations[1:],
                             snapshot.observations + (snapshot.observations[0],)):
            with self.subTest(observations=observations), self.assertRaises(ValueError):
                self.build(replace(snapshot, observations=observations), self.proposal(snapshot))


class PlanProposalTests(PlanningFixtures):
    """Every proposal, including dictionaries, crosses the T13 validation boundary."""

    def test_json_and_dictionary_inputs_receive_identical_validation(self):
        """Reject malformed shapes, actions and unsafe paths through both inputs."""
        snapshot = self.snapshot()
        move = self.proposal(snapshot)["moves"][0]
        invalid = ({}, {"moves": None}, {"moves": [dict(move, action="trash")]},
                   {"moves": [], "approved": True}, {"moves": [dict(move, file_id="unknown")]},
                   {"moves": [dict(move, destination_folder="../outside")]},
                   {"moves": [dict(move, destination_folder="a/b")]},
                   {"moves": [dict(move, destination_folder="Trash")]},
                   {"moves": [dict(move, destination_folder="CON")]},
                   {"moves": [dict(move, destination_folder="file:stream")]},
                   {"moves": [move, move]})
        for value in invalid:
            for proposal in (value, json.dumps(value)):
                with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                    self.build(snapshot, proposal)
        for text in ("not JSON", '{"moves":[],"moves":[]}',
                     '{"moves":[{"file_id":"f1","file_id":"f1","destination_folder":"Docs"}]}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.build(snapshot, text)

    def test_invalid_operations_are_not_hidden_by_empty_selection(self):
        """Deselection cannot turn an invalid proposal into apparently valid data."""
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot)
        proposal["moves"][1]["destination_folder"] = "../escape"
        with self.assertRaises(ValueError):
            self.build(snapshot, proposal, selected_file_ids=())

    def test_selection_rejects_unknown_duplicate_or_malformed_ids(self):
        """Only distinct IDs present in the proposal can be selected."""
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot, {"alpha.txt": "Docs"})
        first = snapshot.files[0].file_id
        for selection in ("f1", ("unknown",), (snapshot.files[1].file_id,),
                          (first, first), (True,), {"file_id": first}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.build(snapshot, proposal, selected_file_ids=selection)

    def test_case_insensitive_duplicate_sources_are_rejected(self):
        """Two IDs cannot disguise the same Windows source name as separate files."""
        snapshot = self.snapshot()
        bad = replace(snapshot, files=(ManifestFile("f1", "alpha.txt", ".txt"),
                                       ManifestFile("f2", "ALPHA.TXT", ".TXT")))
        proposal = {"moves": [{"file_id": "f1", "destination_folder": "Docs"},
                               {"file_id": "f2", "destination_folder": "docs"}]}
        with self.assertRaises(ValueError):
            self.build(bad, proposal)


class PlanFilesystemTests(PlanningFixtures):
    """Current path identities and metadata must still match the trusted scan."""

    def test_occupied_file_or_directory_destination_is_rejected(self):
        """Refuse existing destinations, including Windows case variants."""
        folder = self.root / "Documents"
        folder.mkdir()
        destination = folder / "ALPHA.TXT"
        for kind in ("file", "directory"):
            if kind == "file":
                destination.touch()
            else:
                destination.unlink()
                destination.mkdir()
            snapshot = self.snapshot()
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                self.build(snapshot, self.proposal(snapshot, {"alpha.txt": "Documents"}))

    def test_root_file_cannot_become_a_destination_folder(self):
        """A proposed folder name cannot collide with a source or other root file."""
        snapshot = self.snapshot()
        with self.assertRaises((ValueError, OSError)):
            self.build(snapshot, self.proposal(snapshot, {"beta.jpg": "ALPHA.TXT"}))

    def test_source_content_metadata_changes_invalidate_scan(self):
        """Changed size or modification time requires a fresh scan/review."""
        snapshot = self.snapshot()
        (self.root / "alpha.txt").write_bytes(b"different fixture contents")
        with self.assertRaises(ValueError):
            self.build(snapshot, self.proposal(snapshot))

    def test_source_removal_or_type_change_is_rejected(self):
        """Missing files and files replaced by folders cannot become move plans."""
        snapshot = self.snapshot()
        source = self.root / "alpha.txt"
        source.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.build(snapshot, self.proposal(snapshot))
        source.mkdir()
        with self.assertRaises((ValueError, OSError)):
            self.build(snapshot, self.proposal(snapshot))

    def test_source_replacement_with_same_size_and_mtime_is_rejected(self):
        """File identity detects replacement even when visible metadata matches."""
        snapshot = self.snapshot()
        source = self.root / "alpha.txt"
        source_details = source.lstat()
        root_details = self.root.lstat()
        replacement = self.area / "replacement.txt"
        replacement.write_bytes(b"fixture alpha")
        os.utime(replacement, ns=(source_details.st_atime_ns, source_details.st_mtime_ns))
        source.rename(self.area / "previous-alpha.txt")
        replacement.rename(source)
        os.utime(self.root, ns=(root_details.st_atime_ns, root_details.st_mtime_ns))
        self.assertNotEqual(source.lstat().st_ino, source_details.st_ino)
        with self.assertRaisesRegex(ValueError, "alpha.txt"):
            self.build(snapshot, self.proposal(snapshot))

    def test_changed_root_identity_is_rejected(self):
        """Replacing the selected directory invalidates its captured root identity."""
        snapshot = self.snapshot()
        self.root.rename(self.area / "previous-workspace")
        self.root.mkdir()
        with self.assertRaises(ValueError):
            self.build(snapshot, self.proposal(snapshot))

    def test_root_metadata_change_is_rejected_even_for_empty_selection(self):
        """An empty selection does not bypass root revalidation."""
        snapshot = self.snapshot()
        details = self.root.lstat()
        os.utime(self.root, ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000))
        with self.assertRaises(ValueError):
            self.build(snapshot, self.proposal(snapshot), selected_file_ids=())

    def test_destination_parent_change_is_rejected(self):
        """Changed existing-parent metadata invalidates the captured baseline."""
        folder = self.root / "Documents"
        folder.mkdir()
        snapshot = self.snapshot()
        (folder / "appeared.txt").touch()
        with self.assertRaises(ValueError):
            self.build(snapshot, self.proposal(snapshot, {"alpha.txt": "Documents"}))

    def test_parent_replacement_with_restored_mtime_is_rejected(self):
        """A replacement folder cannot reuse the captured parent identity."""
        folder = self.root / "Documents"
        folder.mkdir()
        snapshot = self.snapshot()
        folder_details = folder.lstat()
        root_details = self.root.lstat()
        folder.rename(self.area / "previous-Documents")
        folder.mkdir()
        os.utime(folder, ns=(folder_details.st_atime_ns, folder_details.st_mtime_ns))
        os.utime(self.root, ns=(root_details.st_atime_ns, root_details.st_mtime_ns))
        self.assertNotEqual(folder.lstat().st_ino, folder_details.st_ino)
        with self.assertRaisesRegex(ValueError, "Documents"):
            self.build(snapshot, self.proposal(snapshot, {"alpha.txt": "Documents"}))

    def test_occupied_destination_is_not_hidden_by_deselection(self):
        """The whole proposal must be valid before any subset can be reviewed."""
        folder = self.root / "Documents"
        folder.mkdir()
        (folder / "alpha.txt").touch()
        snapshot = self.snapshot()
        with self.assertRaises((ValueError, OSError)):
            self.build(snapshot, self.proposal(snapshot),
                       selected_file_ids=(snapshot.files[1].file_id,))

    def test_new_parent_appearing_after_scan_is_not_silently_adopted(self):
        """A proposed missing parent cannot become an unobserved existing parent."""
        snapshot = self.snapshot()
        (self.root / "Documents").mkdir()
        with self.assertRaises((ValueError, OSError)):
            self.build(snapshot, self.proposal(snapshot))

    def test_hard_link_added_after_scan_is_rejected(self):
        """Sources that become multiply linked are no longer eligible."""
        snapshot = self.snapshot()
        (self.area / "outside-alias.txt").hardlink_to(self.root / "alpha.txt")
        with self.assertRaises(ValueError):
            self.build(snapshot, self.proposal(snapshot))

    def test_redirected_root_ancestor_source_or_parent_is_rejected(self):
        """Shared validation must still refuse redirects at every relevant level."""
        folder = self.root / "Documents"
        folder.mkdir()
        snapshot = self.snapshot()
        proposal = self.proposal(snapshot, {"alpha.txt": "Documents"})
        for target in (self.area, self.root, self.root / "alpha.txt", folder):
            with self.subTest(target=target):
                with patch.object(Path, "is_junction", new=lambda path: path == target):
                    with self.assertRaises((ValueError, OSError)):
                        self.build(snapshot, proposal)

    def test_inaccessible_source_is_an_explicit_failure(self):
        """Permission errors cannot silently remove a proposed source from review."""
        snapshot = self.snapshot()
        original_lstat = Path.lstat

        def inaccessible(path, *args, **kwargs):
            if path == self.root / "alpha.txt":
                raise PermissionError("Access denied")
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", new=inaccessible):
            with self.assertRaises((ValueError, OSError)):
                self.build(snapshot, self.proposal(snapshot))


class ExecutionFixtures(PlanningFixtures):
    """Execution uses only disposable files and isolated app-owned journal state."""

    def setUp(self):
        super().setUp()
        self.state = self.area / "app-state"

    def plan(self, destinations=None, selected_file_ids=None):
        snapshot = self.snapshot()
        return self.build(snapshot, self.proposal(snapshot, destinations),
                          selected_file_ids=selected_file_ids)

    def approval(self, plan):
        return approve_organization_plan(plan, workspace=self.root,
                                         scan_id=plan.scan_id, confirmed=True)

    def execute(self, plan, **kwargs):
        if "approval" not in kwargs:
            kwargs["approval"] = self.approval(plan)
        return execute_organization_plan(self.root, plan, scan_id=plan.scan_id,
                                         state_directory=self.state, **kwargs)

    def journals(self):
        with recovery.ExecutionLock(self.root, self.state) as lock:
            return recovery.load_journals(lock)


class ExecutionApprovalTests(ExecutionFixtures):
    """Only trusted explicit approval for the exact app-owned plan permits effects."""

    def test_unapproved_requests_do_not_create_folders_or_journals(self):
        """Neither a Boolean nor AI-shaped data can act as an approval token."""
        plan = self.plan()
        for approval in (None, True, {"approved": True}, object()):
            result = execute_organization_plan(self.root, plan, scan_id=plan.scan_id,
                                               approval=approval, state_directory=self.state)
            self.assertEqual(result.status, "rejected")
        self.assertFalse(self.state.exists())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"alpha.txt", "beta.jpg"})

    def test_approval_requires_confirmation_and_a_plan_from_the_app(self):
        """Handmade/copy plans and truthy confirmation values cannot mint approval."""
        plan = self.plan()
        for confirmed in (False, 1, "yes", None):
            with self.subTest(confirmed=confirmed), self.assertRaises(ValueError):
                approve_organization_plan(plan, workspace=self.root,
                                          scan_id=plan.scan_id, confirmed=confirmed)
        with self.assertRaises(ValueError):
            self.approval(replace(plan))

    def test_approval_is_bound_to_current_root_scan_and_exact_selection(self):
        """Old approval cannot authorize another scan, root, plan or selected subset."""
        plan = self.plan()
        changed = self.plan(selected_file_ids=("f1",))
        approval = self.approval(plan)
        result = self.execute(changed, approval=approval)
        self.assertEqual(result.status, "rejected")
        for workspace, scan_id in ((self.area, plan.scan_id), (self.root, "different-scan")):
            plan = self.plan()
            token = self.approval(plan)
            result = execute_organization_plan(workspace, plan, scan_id=scan_id,
                                               approval=token, state_directory=self.state)
            self.assertEqual(result.status, "rejected")
        self.assertFalse(self.state.exists())

    def test_approval_factory_refuses_stale_root_or_scan_context(self):
        """Confirmation is refused before issuance when the current context changed."""
        plan = self.plan()
        for root, scan_id in ((self.area, plan.scan_id), (self.root, "old-scan")):
            with self.subTest(root=root), self.assertRaises(ValueError):
                approve_organization_plan(plan, workspace=root, scan_id=scan_id, confirmed=True)

    def test_arbitrary_cancellation_objects_cannot_supply_callbacks(self):
        """Only threading.Event is accepted; arbitrary is_set methods never run."""
        event = SimpleNamespace(is_set=Mock(side_effect=AssertionError("callback")))
        result = self.execute(self.plan(), cancel_event=event)
        self.assertEqual(result.status, "rejected")
        event.is_set.assert_not_called()

    def test_mutated_plan_invalidates_previously_issued_approval(self):
        """Even illicit in-place changes to frozen review data invalidate approval."""
        plan = self.plan()
        token = self.approval(plan)
        object.__setattr__(plan, "operations", plan.operations[:1])
        result = self.execute(plan, approval=token)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(self.state.exists())

    def test_approval_is_consumed_once_and_success_cannot_be_replayed(self):
        """Repeated execution or reapproval of the same review plan is rejected."""
        plan = self.plan()
        approval = self.approval(plan)
        first = self.execute(plan, approval=approval)
        self.assertEqual(first.status, "success")
        second = self.execute(plan, approval=approval)
        self.assertEqual(second.status, "rejected")
        with self.assertRaises(ValueError):
            self.approval(plan)
        self.assertEqual(len(self.journals()), 1)

    def test_empty_plan_is_a_noop_without_state_or_managed_writes(self):
        """An approved empty selection needs no journal or storage mutation."""
        plan = self.plan(selected_file_ids=())
        result = self.execute(plan)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.completed, ())
        self.assertFalse(self.state.exists())

    def test_cancellation_before_start_creates_no_journal_or_folder(self):
        """A trusted Event set before execution performs no filesystem mutation."""
        event = threading.Event()
        event.set()
        result = self.execute(self.plan(), cancel_event=event)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.recovery_required)
        self.assertFalse(self.state.exists())


class ExecutionStorageTests(ExecutionFixtures):
    """Windows media/filesystem queries and the app lock precede mutations."""

    def test_unsupported_storage_fails_closed(self):
        """Reject remote/removable media, non-NTFS, read-only and query failures."""
        for info in ((4, "NTFS", 0), (2, "NTFS", 0), (3, "exFAT", 0),
                     (3, "NTFS", 0x80000), OSError("query failed")):
            with self.subTest(info=info):
                plan = self.plan()
                mock = Mock(side_effect=info) if isinstance(info, Exception) else Mock(return_value=info)
                with patch.object(operations, "_volume_info", mock):
                    result = self.execute(plan)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.completed, ())
                self.assertFalse((self.root / "Documents").exists())

    def test_real_fixed_ntfs_preflight_and_no_replace_primitive(self):
        """Exercise actual Windows volume queries and refuse an occupied target."""
        operations._preflight_storage(self.root)
        destination = self.root / "occupied.txt"
        destination.write_bytes(b"preserve destination")
        with self.assertRaises(OSError):
            operations._move_no_replace(self.root / "alpha.txt", destination)
        self.assertEqual(destination.read_bytes(), b"preserve destination")
        self.assertEqual((self.root / "alpha.txt").read_bytes(), b"fixture alpha")

    def test_move_primitive_never_sets_replace_or_copy_flags(self):
        """The only native move call uses flags zero and no fallback path."""
        native = SimpleNamespace(MoveFileExW=Mock(return_value=1))
        with patch.object(operations, "_windows_api", return_value=native):
            operations._move_no_replace(self.root / "alpha.txt", self.root / "Documents/alpha.txt")
        self.assertEqual(native.MoveFileExW.call_count, 1)
        self.assertEqual(native.MoveFileExW.call_args.args[2], 0)

    def test_competing_execution_lock_blocks_the_entire_batch(self):
        """An existing app lock prevents all organization mutations."""
        plan = self.plan()
        with recovery.ExecutionLock(self.root, self.state):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.completed, ())
        self.assertFalse((self.root / "Documents").exists())

    def test_corrupt_and_unresolved_journals_block_new_execution(self):
        """No executor may bypass unfinished or unreadable evidence."""
        old = self.plan()
        with recovery.ExecutionLock(self.root, self.state) as lock:
            recovery.begin_batch(lock, old)
        result = self.execute(self.plan())
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.completed, ())
        path = self.state / f"journal-{old.plan_id}.json"
        path.write_text("{bad", encoding="utf-8")
        result = self.execute(self.plan())
        self.assertTrue(result.recovery_required)
        self.assertEqual(path.read_text(), "{bad")


class ExecutionSuccessTests(ExecutionFixtures):
    """Selected folder creations and same-volume moves preserve fixture bytes."""

    def test_new_folders_and_moves_are_journaled_in_order(self):
        """Successful creation/moves publish observed identities for every operation."""
        plan = self.plan()
        result = self.execute(plan)
        self.assertEqual(result.status, "success")
        self.assertEqual(tuple(item.plan_id for item in result.completed),
                         tuple(op.plan_id for op in plan.operations))
        self.assertEqual(result.not_started, ())
        self.assertEqual(result.created_folders, ("Documents", "Pictures"))
        self.assertEqual((self.root / "Documents/alpha.txt").read_bytes(), b"fixture alpha")
        self.assertEqual((self.root / "Pictures/beta.jpg").read_bytes(), b"fixture beta")
        self.assertEqual([record.status for record in self.journals()[0].records], ["success"] * 4)

    def test_shared_existing_folder_accepts_only_expected_metadata_changes(self):
        """Two moves can change root/parent metadata while keeping their identities."""
        folder = self.root / "Documents"
        folder.mkdir()
        (folder / "keep.txt").write_bytes(b"keep fixture")
        identity = folder.stat().st_ino
        plan = self.plan({"alpha.txt": "Documents", "beta.jpg": "Documents"})
        result = self.execute(plan)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.created_folders, ())
        self.assertEqual(folder.stat().st_ino, identity)
        self.assertEqual((folder / "keep.txt").read_bytes(), b"keep fixture")
        self.assertEqual({p.name for p in folder.iterdir()}, {"alpha.txt", "beta.jpg", "keep.txt"})

    def test_shared_new_folder_and_subset_execute_only_reviewed_moves(self):
        """A selected subset creates only its needed folder and preserves other files."""
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"}, selected_file_ids=("f2",))
        result = self.execute(plan)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.created_folders, ("Docs",))
        self.assertTrue((self.root / "alpha.txt").is_file())
        self.assertEqual((self.root / "Docs/beta.jpg").read_bytes(), b"fixture beta")

    def test_multiple_moves_share_the_observed_created_directory(self):
        """Subsequent move intents retain the created parent's recorded identity."""
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        result = self.execute(plan)
        self.assertEqual(result.status, "success")
        journal = self.journals()[0]
        identity = journal.records[0].outcome_observations[0].file_id
        for record in journal.records[1:]:
            parent = next(item for item in record.intent_observations if item.relative_path == "Docs")
            self.assertEqual(parent.file_id, identity)

    def test_no_content_reads_shell_ai_or_copy_delete_fallback(self):
        """Only app-state JSON is opened; native moves preserve the original bytes."""
        plan = self.plan()
        original_io, original_os_open = io.open, os.open

        def guarded_io(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path).is_relative_to(self.root):
                raise AssertionError("managed content read")
            return original_io(path, *args, **kwargs)

        def guarded_os(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path).is_relative_to(self.root):
                raise AssertionError("managed content descriptor")
            return original_os_open(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch("io.open", side_effect=guarded_io))
            stack.enter_context(patch("builtins.open", side_effect=guarded_io))
            stack.enter_context(patch("os.open", side_effect=guarded_os))
            for name in ("rename", "replace"):
                stack.enter_context(patch.object(Path, name, side_effect=AssertionError(name)))
            stack.enter_context(patch("subprocess.run", side_effect=AssertionError("shell")))
            stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("shell")))
            stack.enter_context(patch("file_assistant.ollama_client.HTTPConnection", side_effect=AssertionError("AI")))
            result = self.execute(plan)
        self.assertEqual(result.status, "success")
        self.assertEqual((self.root / "Documents/alpha.txt").read_bytes(), b"fixture alpha")


class ExecutionValidationTests(ExecutionFixtures):
    """Batch and immediate checks reject stale paths without refreshing evidence."""

    def test_entire_batch_is_checked_before_first_creation(self):
        """An occupied final target prevents all earlier proposed mkdir operations."""
        (self.root / "Existing").mkdir()
        plan = self.plan({"alpha.txt": "New", "beta.jpg": "Existing"})
        (self.root / "Existing/beta.jpg").write_bytes(b"existing fixture")
        result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertFalse((self.root / "New").exists())
        self.assertEqual(result.not_started, tuple(op.plan_id for op in plan.operations))

    def test_unsupported_actions_and_changed_requests_are_refused(self):
        """Shared-model action values and changed destinations do not gain authority."""
        for request in (operations.OperationRequest("trash", source="alpha.txt"),
                        operations.OperationRequest("move", "alpha.txt", "../escape.txt")):
            plan = self.plan()
            approval = self.approval(plan)
            object.__setattr__(plan.operations[-1], "request", request)
            result = self.execute(plan, approval=approval)
            self.assertEqual(result.status, "rejected")
            self.assertFalse((self.root / "Documents").exists())

    def test_changed_source_root_parent_and_hard_links_are_rejected(self):
        """Original identities and metadata remain mandatory before execution."""
        (self.root / "Documents").mkdir()
        plan = self.plan({"alpha.txt": "Documents"})
        (self.area / "alias.txt").hardlink_to(self.root / "alpha.txt")
        result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertTrue((self.root / "alpha.txt").is_file())

    def test_source_replacement_with_restored_metadata_is_rejected(self):
        """Same-sized same-timestamp replacements cannot reuse a source identity."""
        plan = self.plan()
        source = self.root / "alpha.txt"
        source_details, root_details = source.stat(), self.root.stat()
        source.rename(self.area / "previous-alpha.txt")
        source.write_bytes(b"fixture alpha")
        os.utime(source, ns=(source_details.st_atime_ns, source_details.st_mtime_ns))
        os.utime(self.root, ns=(root_details.st_atime_ns, root_details.st_mtime_ns))
        result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.completed, ())
        self.assertFalse((self.root / "Documents").exists())

    def test_root_replacement_cannot_redirect_an_approved_batch(self):
        """A replacement selected directory never inherits approval from its path."""
        plan = self.plan()
        token = self.approval(plan)
        self.root.rename(self.area / "previous-root")
        self.root.mkdir()
        result = self.execute(plan, approval=token)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual((self.area / "previous-root/alpha.txt").read_bytes(), b"fixture alpha")

    def test_parent_replacement_with_restored_root_mtime_is_rejected(self):
        """An existing destination parent must retain its original identity."""
        folder = self.root / "Docs"
        folder.mkdir()
        plan = self.plan({"alpha.txt": "Docs"})
        root_details = self.root.stat()
        folder.rename(self.area / "previous-Docs")
        folder.mkdir()
        os.utime(self.root, ns=(root_details.st_atime_ns, root_details.st_mtime_ns))
        result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertTrue((self.root / "alpha.txt").is_file())

    def test_inaccessible_source_is_an_explicit_batch_rejection(self):
        """Permission failure during revalidation cannot silently omit a move."""
        plan = self.plan()
        original = Path.lstat

        def denied(path, *args, **kwargs):
            if path == self.root / "alpha.txt":
                raise PermissionError("synthetic denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", new=denied):
            result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertFalse((self.root / "Documents").exists())

    def test_missing_source_and_permission_errors_prevent_first_mutation(self):
        """Missing and inaccessible paths are explicit batch rejections."""
        plan = self.plan()
        (self.root / "beta.jpg").unlink()
        result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertFalse((self.root / "Documents").exists())

    def test_redirected_root_source_and_parent_are_never_followed(self):
        """Existing shared validators reject links at every relevant boundary."""
        (self.root / "Documents").mkdir()
        for target in (self.root, self.root / "alpha.txt", self.root / "Documents"):
            plan = self.plan({"alpha.txt": "Documents"})
            with patch.object(Path, "is_junction", new=lambda p: p == target):
                result = self.execute(plan)
            self.assertNotEqual(result.status, "success")
            self.assertTrue((self.root / "alpha.txt").is_file())

    def test_cross_volume_source_is_rejected_before_mutation(self):
        """A stat device mismatch cannot enter a copy/delete move path."""
        plan = self.plan()
        original = operations._inspect_component

        def changed(path):
            details = original(path)
            if path == self.root / "alpha.txt":
                values = {name: getattr(details, name) for name in dir(details) if name.startswith("st_")}
                values["st_dev"] += 1
                return SimpleNamespace(**values)
            return details

        with patch.object(operations, "_inspect_component", side_effect=changed):
            result = self.execute(plan)
        self.assertEqual(result.status, "rejected")
        self.assertFalse((self.root / "Documents").exists())

    def test_unexpected_changes_during_our_move_are_not_blindly_refreshed(self):
        """An unrelated new root entry in the mutation window stops the batch."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations._move_no_replace

        def changed(source, destination):
            original(source, destination)
            (self.root / "unexpected.txt").touch()

        with patch.object(operations, "_move_no_replace", side_effect=changed):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertTrue((self.root / "beta.jpg").is_file())
        self.assertFalse((self.root / "Docs/beta.jpg").exists())

    def test_parent_replacement_between_moves_invalidates_the_next_operation(self):
        """A replacement parent is never adopted as a new expected baseline."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations.record_outcome

        def replaced(*args, **kwargs):
            value = original(*args, **kwargs)
            (self.root / "Docs").rename(self.area / "previous-Docs")
            (self.root / "Docs").mkdir()
            return value

        with patch.object(operations, "record_outcome", side_effect=replaced):
            result = self.execute(plan)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.recovery_required)
        self.assertEqual(len(result.completed), 1)
        self.assertTrue((self.root / "beta.jpg").is_file())

    def test_metadata_tampering_during_our_move_is_not_accepted_as_our_change(self):
        """Only directory size/mtime changes are expected, not file mode changes."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations._move_no_replace

        def changed(source, destination):
            original(source, destination)
            destination.chmod(stat.S_IREAD)

        try:
            with patch.object(operations, "_move_no_replace", side_effect=changed):
                result = self.execute(plan)
            self.assertTrue(result.recovery_required)
            self.assertTrue((self.root / "beta.jpg").is_file())
        finally:
            destination = self.root / "Docs/alpha.txt"
            if destination.exists():
                destination.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_root_attribute_change_is_not_refreshed_as_a_directory_timestamp_change(self):
        """Root identity alone does not excuse changed attributes during a move."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations._move_no_replace

        def changed(source, destination):
            original(source, destination)
            self.root.chmod(stat.S_IREAD)

        try:
            with patch.object(operations, "_move_no_replace", side_effect=changed):
                result = self.execute(plan)
            self.assertTrue(result.recovery_required)
            self.assertTrue((self.root / "beta.jpg").is_file())
        finally:
            self.root.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_source_replacement_during_intent_persistence_blocks_native_move(self):
        """Revalidate again after intent publication before invoking Windows."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs"})
        original = operations.record_intent

        def replaced(*args, **kwargs):
            value = original(*args, **kwargs)
            source = self.root / "alpha.txt"
            source.rename(self.area / "previous-alpha.txt")
            source.write_bytes(b"replacement fixture")
            return value

        with patch.object(operations, "record_intent", side_effect=replaced):
            result = self.execute(plan)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.recovery_required)
        self.assertEqual(result.not_started, tuple(op.plan_id for op in plan.operations))
        self.assertFalse((self.root / "Docs/alpha.txt").exists())
        self.assertEqual(self.journals()[0].records[0].status, "not_executed")


class ExecutionFailureTests(ExecutionFixtures):
    """Failures preserve evidence and report effects versus untouched operations."""

    def test_destination_appearing_at_native_move_is_never_overwritten(self):
        """The Windows primitive refuses a conflict after all Python validation."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations._move_no_replace

        def occupied(source, destination):
            destination.write_bytes(b"racing fixture")
            original(source, destination)

        with patch.object(operations, "_move_no_replace", side_effect=occupied):
            result = self.execute(plan)
        self.assertNotEqual(result.status, "success")
        self.assertEqual(result.completed, ())
        self.assertEqual((self.root / "Docs/alpha.txt").read_bytes(), b"racing fixture")
        self.assertEqual((self.root / "alpha.txt").read_bytes(), b"fixture alpha")
        self.assertTrue((self.root / "beta.jpg").is_file())

    def test_new_folder_appearing_at_creation_is_a_conflict(self):
        """An unexpectedly existing child folder is never silently reused."""
        plan = self.plan()
        original = operations._create_directory

        def occupied(path):
            path.mkdir()
            original(path)

        with patch.object(operations, "_create_directory", side_effect=occupied):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.completed, ())
        self.assertTrue((self.root / "alpha.txt").is_file())

    def test_permission_error_stops_without_running_later_operations(self):
        """A failed first native call cannot cause fallback or subsequent moves."""
        plan = self.plan()
        with patch.object(operations, "_create_directory", side_effect=PermissionError("denied")):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.completed, ())
        self.assertFalse((self.root / "Documents").exists())
        self.assertEqual(self.journals()[0].records[0].status, "recovery_required")

    def test_intent_failure_prevents_the_managed_mutation(self):
        """The operation is untouched if journal fsync/replace cannot publish intent."""
        plan = self.plan()
        original = operations.record_intent

        def failed(*args, **kwargs):
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                return original(*args, **kwargs)

        with patch.object(operations, "record_intent", side_effect=failed):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.not_started, tuple(op.plan_id for op in plan.operations))
        self.assertFalse((self.root / "Documents").exists())
        self.assertEqual(self.journals()[0].records[0].status, "pending")

    def test_outcome_failure_after_creation_keeps_durable_intent_and_empty_folder(self):
        """An actual mkdir with unpublished outcome is uncertain and is not rolled back."""
        plan = self.plan()
        original = operations.record_outcome

        def failed(*args, **kwargs):
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                return original(*args, **kwargs)

        with patch.object(operations, "record_outcome", side_effect=failed):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.created_folders, ("Documents",))
        self.assertEqual(result.stopped.status, "recovery_required")
        self.assertTrue((self.root / "Documents").is_dir())
        self.assertEqual(list((self.root / "Documents").iterdir()), [])
        self.assertEqual(self.journals()[0].records[0].status, "intent")

    def test_outcome_failure_after_move_preserves_observable_destination_identity(self):
        """Unpublished move outcomes remain inspectable as destination-only evidence."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations.record_outcome

        def failed(*args, **kwargs):
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                return original(*args, **kwargs)

        with patch.object(operations, "record_outcome", side_effect=failed):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.completed, ())
        self.assertEqual(result.not_started, (plan.operations[-1].plan_id,))
        with recovery.ExecutionLock(self.root, self.state) as lock:
            findings = recovery.inspect_recovery(lock)
        self.assertEqual([finding.conclusion for finding in findings], ["destination_only", "not_started"])

    def test_cancel_during_intent_write_keeps_intent_but_performs_no_mutation(self):
        """A published intent cancelled before submission becomes safely terminal."""
        plan = self.plan()
        event = threading.Event()
        original = operations.record_intent

        def cancelled(*args, **kwargs):
            value = original(*args, **kwargs)
            event.set()
            return value

        with patch.object(operations, "record_intent", side_effect=cancelled):
            result = self.execute(plan, cancel_event=event)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.recovery_required)
        self.assertFalse((self.root / "Documents").exists())
        self.assertEqual(self.journals()[0].records[0].status, "not_executed")
        self.assertTrue(self.journals()[0].records[0].intent_observations)
        self.assertEqual(self.execute(self.plan()).status, "success")

    def test_cancellation_between_operations_preserves_pending_evidence(self):
        """Retained terminal remainder allows a fresh plan after ordinary cancellation."""
        plan = self.plan()
        event = threading.Event()
        original = operations.record_outcome

        def cancelled(*args, **kwargs):
            value = original(*args, **kwargs)
            event.set()
            return value

        with patch.object(operations, "record_outcome", side_effect=cancelled):
            result = self.execute(plan, cancel_event=event)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.recovery_required)
        self.assertEqual(len(result.completed), 1)
        self.assertEqual(result.not_started, tuple(op.plan_id for op in plan.operations[1:]))
        self.assertEqual(result.created_folders, ("Documents",))
        self.assertEqual([r.status for r in self.journals()[0].records],
                         ["success", "not_executed", "not_executed", "not_executed"])
        self.assertTrue((self.root / "Documents").is_dir())
        fresh = self.plan()
        self.assertEqual(self.execute(fresh).status, "success")
        self.assertEqual(len(self.journals()), 2)

    def test_cancel_finalization_failure_preserves_blocking_evidence(self):
        """Failed cancellation publication cannot release an unresolved batch."""
        plan = self.plan()
        event = threading.Event()
        original = operations.record_outcome

        def cancelled(*args, **kwargs):
            value = original(*args, **kwargs)
            event.set()
            return value

        with patch.object(operations, "record_outcome", side_effect=cancelled):
            with patch.object(operations, "finish_unexecuted", side_effect=recovery.PersistenceError("disk error")):
                result = self.execute(plan, cancel_event=event)
        self.assertTrue(result.recovery_required)
        self.assertEqual([record.status for record in self.journals()[0].records],
                         ["success", "pending", "pending", "pending"])
        self.assertTrue(self.execute(self.plan()).recovery_required)

    def test_uncertain_move_requires_explicit_resolution_before_fresh_execution(self):
        """An unpublished move stays blocked until its exact recovery evidence is acknowledged."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations.record_outcome

        def failed(*args, **kwargs):
            with patch("file_assistant.recovery.os.replace", side_effect=OSError("disk error")):
                return original(*args, **kwargs)

        with patch.object(operations, "record_outcome", side_effect=failed):
            result = self.execute(plan)
        self.assertTrue(result.recovery_required)
        self.assertTrue(self.execute(self.plan({"beta.jpg": "Docs"})).recovery_required)
        with recovery.ExecutionLock(self.root, self.state) as lock:
            journal = recovery.load_journals(lock)[0]
            findings = recovery.inspect_recovery(lock)
            recovery.resolve_recovery(lock, journal, findings, confirmed=True,
                                      message="Reviewed moved alpha and untouched beta; close old batch.")
        self.assertEqual(self.execute(self.plan({"beta.jpg": "Docs"})).status, "success")
        retained = next(item for item in self.journals() if item.plan.plan_id == plan.plan_id)
        self.assertEqual(retained.records[0].status, "intent")
        self.assertIsNotNone(retained.resolution)

    def test_restart_inspection_after_interruption_preserves_move_evidence(self):
        """An abrupt interruption after a move leaves intent for metadata-only inspection."""
        (self.root / "Docs").mkdir()
        plan = self.plan({"alpha.txt": "Docs", "beta.jpg": "Docs"})
        original = operations._move_no_replace

        def interrupted(source, destination):
            original(source, destination)
            raise KeyboardInterrupt("simulated interruption")

        with patch.object(operations, "_move_no_replace", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.execute(plan)
        with recovery.ExecutionLock(self.root, self.state) as lock:
            findings = recovery.inspect_recovery(lock)
        self.assertEqual([item.conclusion for item in findings], ["destination_only", "not_started"])
        result = self.execute(self.plan({"beta.jpg": "Docs"}))
        self.assertTrue(result.recovery_required)
        self.assertTrue((self.root / "beta.jpg").is_file())
