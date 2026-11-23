"""UI adapters for trusted configuration, planning, execution and recovery."""

from pathlib import Path

from .. import config, ollama_client, operations, recovery
from ..diagnostics import DiagnosticStore
from ..workspace import validate_workspace


class Services:
    """Small adapter over existing APIs, with one trusted app-state location."""

    def __init__(self, state_directory=None):
        self.state_directory = (Path(state_directory) if state_directory is not None
                                else config.default_config_path().parent)
        try:
            self.diagnostics = DiagnosticStore()
        except Exception:
            self.diagnostics = None

    def choose_root(self, root):
        root = validate_workspace(Path(root))
        config.save_config(config.AppConfig(root), self.state_directory / "config.json")
        return root

    def inspect(self, root):
        with recovery.ExecutionLock(root, self.state_directory) as lock:
            journals = tuple(j for j in recovery.load_journals(lock) if not recovery.is_complete(j))
            return journals, recovery.inspect_recovery(lock)

    def acknowledge(self, root, review):
        journals, findings = review
        if self.inspect(root) != review:
            raise recovery.RecoveryRequired("Recovery findings changed; inspect again.")
        # The global journal directory can contain other previously chosen roots.
        # Every journal and root is shown in the separate confirmation. The backend
        # rechecks each exact journal/findings under its own trusted root context.
        for journal in journals:
            with recovery.ExecutionLock(journal.plan.root, self.state_directory) as lock:
                recovery.resolve_recovery(
                    lock, journal, tuple(f for f in findings if f.batch_id == journal.plan.plan_id),
                    confirmed=True, message="User explicitly acknowledged the displayed recovery findings; no retry or resume.",
                )

    scan = staticmethod(operations.scan_files)
    plan = staticmethod(operations.build_organization_plan)
    approve = staticmethod(operations.approve_organization_plan)

    def begin_diagnostic(self, request, model, *, revision, scan_id=None):
        try:
            return self.diagnostics.begin(request, model, revision=revision, scan_id=scan_id)
        except Exception:
            return None

    def converse(self, manifest, request, *, model, history, diagnostic=None):
        return ollama_client.request_conversation(manifest, request, model=model, history=history, diagnostic=diagnostic)

    def execute(self, root, plan, **kwargs):
        return operations.execute_organization_plan(root, plan, state_directory=self.state_directory, **kwargs)
