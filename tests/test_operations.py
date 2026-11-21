import builtins
import io
import json
import os
import stat
import subprocess
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from file_assistant.models import Entry, OrganizationPlan, PathObservation, ScanResult
from file_assistant.operations import (
    ManifestFile, build_organization_plan, list_entries, scan_files, search_names,
)


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
