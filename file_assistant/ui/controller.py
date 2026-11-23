"""Conversation state, revision checks and explicit approval lifecycle.

Used only on the UI thread. Worker closures capture context and return values;
they never update the controller or widgets.
"""

import copy
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import ollama_client
from .formatting import format_report


@dataclass
class ReviewCard:
    plan: object
    proposal: dict
    selected: tuple[str, ...]
    status: str = "Current"


class ConversationController:
    """Revision-bound session and approval lifecycle, independent of Tk widgets."""

    def __init__(self, services, worker):
        self.services, self.worker = services, worker
        self.state = "no_folder"
        self.root = None
        self.model = ""
        self.model_status = "Choose an installed local model"
        self.snapshot = None
        self.history = []
        self.draft = ""
        self.messages = [("Application", "Choose a folder, then tell me how you want its files organized. "
                          "I use filenames only. Nothing moves until you review and confirm.")]
        self.cards = []
        self.timeline = [("message", self.messages[0])]
        self.plan = None
        self.proposal = None
        self.confirmation = None
        self.recovery_review = None
        self.recovery_confirmation = None
        self.recovery_blocked = False
        self.report = None
        self.result_plan = None
        self.busy = False
        self.closing = self.closed = False
        self.cancel_event = threading.Event()
        self.revision = 0
        self._pending = None
        self.on_change = lambda: None

    def _message(self, role, text):
        pair = (role, str(text))
        self.messages.append(pair)
        self.timeline.append(("message", pair))

    def _changed(self):
        self.on_change()

    def _invalidate(self, *, clear_scan=False):
        self.revision += 1
        for card in self.cards:
            if card.status == "Current":
                card.status = "Superseded"
        self.plan = self.proposal = self.confirmation = None
        self.recovery_confirmation = None
        if clear_scan:
            self.snapshot = None
            self.history = []

    def _start(self, work, accept, *, deferred=False, failure=None):
        token = self.revision
        if self.busy:
            if deferred:
                self._pending = (work, accept, token, failure)
                self._changed()
                return True
            return False
        self.busy = True
        self.cancel_event = threading.Event()

        def done(value, error):
            self.busy = False
            if token == self.revision and (not self.closing or self.state == "applying"):
                if error is not None:
                    if failure is not None:
                        failure(error)
                    else:
                        self.state = "blocked"
                        self._message("Application", f"Blocked: {error}")
                else:
                    accept(value)
            if self.closing:
                self._pending = None
                self.closed = True
            elif self._pending is not None:
                pending_work, pending_accept, pending_token, pending_failure = self._pending
                self._pending = None
                if pending_token == self.revision:
                    self._start(pending_work, pending_accept, failure=pending_failure)
            self._changed()

        self.worker.submit(work, done)
        self._changed()
        return True

    def select_root(self, root):
        if self.state == "applying" or self.closing:
            return False
        self._invalidate(clear_scan=True)
        self.root = None
        self.recovery_review = None
        self.recovery_blocked = True
        self.state = "thinking"
        picked = Path(root)

        def work():
            validated = self.services.choose_root(picked)
            try:
                review = self.services.inspect(validated)
            except Exception as exc:
                return validated, None, exc
            return validated, review, None

        def accept(value):
            self.root, review, error = value
            if error:
                self.recovery_blocked = True
                self.state = "blocked"
                self._message("Application", f"Recovery inspection blocked: {error}. Records are retained.")
            else:
                self._accept_recovery(review)

        return self._start(work, accept, deferred=True)

    def _accept_recovery(self, review):
        self.recovery_review = review if review[0] or review[1] else None
        self.recovery_blocked = self.recovery_review is not None
        self.state = "blocked" if self.recovery_blocked else "ready"
        if self.recovery_blocked:
            self._message("Application", "Recovery evidence blocks new work. Inspect it and explicitly acknowledge "
                          "the current findings using the separate recovery controls. Nothing is retried or resumed.")

    def select_model(self, model):
        if self.busy or self.closing or self.state == "applying":
            return False
        if self.model != model:
            self._invalidate(clear_scan=True)
            self.model = model.strip()
            self.model_status = "Not checked; verified locally when you send"
            self.state = "blocked" if self.recovery_blocked else ("ready" if self.root else "no_folder")
            self._changed()
        return True

    def rescan(self):
        if self.root is None or self.closing or self.state == "applying":
            return False
        self._invalidate(clear_scan=True)
        self.recovery_blocked = True
        self.recovery_review = None
        self.state = "thinking"
        root = self.root

        def work():
            review = self.services.inspect(root)
            snapshot = self.services.scan(root) if not (review[0] or review[1]) else None
            if snapshot is not None:
                snapshot.manifest()  # Fail closed on partial/oversize scans.
            return review, snapshot

        def accept(value):
            review, snapshot = value
            self._accept_recovery(review)
            self.snapshot = snapshot

        return self._start(work, accept, deferred=True)

    def send(self, request):
        if self.busy or self.closing or self.state == "applying" or self.recovery_blocked:
            return False
        self.draft = request
        if self.root is None:
            self._message("Application", "Choose a folder first. Your unsent request stays here.")
            self._changed()
            return False
        # A follow-up removes the earlier review even if validation/inference fails.
        self._invalidate()
        try:
            if not self.model:
                raise ValueError("Select the exact name of an installed local Ollama model first.")
            if (not isinstance(request, str) or not request.strip()
                    or len(request) > ollama_client.MAX_USER_MESSAGE_CHARS
                    or any(ord(c) < 32 and c not in "\n\r\t" for c in request)):
                raise ValueError("Use a nonempty request of at most 2,000 characters.")
            if len(self.history) >= ollama_client.MAX_CONVERSATION_EXCHANGES:
                raise ValueError("Conversation is full. Rescan and restate all current preferences.")
            context = {"request": request, "history": self.history}
            if len(json.dumps(context, ensure_ascii=True).encode("utf-8")) > ollama_client.MAX_CONTEXT_BYTES:
                raise ValueError("Conversation byte budget is full. Rescan and restate all current preferences.")
        except (ValueError, UnicodeError) as exc:
            self.state = "blocked"
            self._message("Application", str(exc))
            self._changed()
            return False
        root, snapshot, model = self.root, self.snapshot, self.model
        history = tuple(copy.deepcopy(self.history))
        event = threading.Event()
        self.state = "thinking"
        self.model_status = "Scanning / asking local model"
        self._message("You", request)
        self.draft = ""

        def work():
            current = snapshot if snapshot is not None else self.services.scan(root)
            manifest = current.manifest()
            # Captured BEFORE inference. Repeating IDs cannot bind a late response
            # to a newer scan. Completion additionally checks the UI revision.
            scan_id = current.scan_id
            if event.is_set():
                return None
            reply = self.services.converse(manifest, request, model=model, history=history)
            # The real service validates this envelope. Also fail closed for an
            # accidentally malformed service adapter, without interpreting prose.
            if (type(reply) is not dict or set(reply) != {"kind", "message", "moves"}
                    or reply["kind"] not in ("clarification", "proposal")
                    or not isinstance(reply["message"], str) or not reply["message"].strip()
                    or len(reply["message"]) > ollama_client.MAX_ASSISTANT_MESSAGE_CHARS):
                raise ValueError("Invalid conversation reply.")
            moves = ollama_client.parse_proposal(json.dumps({"moves": reply["moves"]}), manifest)
            if reply["kind"] == "clarification" and moves["moves"]:
                raise ValueError("Clarifications cannot contain moves.")
            complete_history = [*history, {"request": request, "reply": reply}]
            if len(json.dumps({"history": complete_history}, ensure_ascii=True).encode("utf-8")) > ollama_client.MAX_CONTEXT_BYTES:
                raise ValueError("Completed reply exceeds the conversation budget. Rescan and restate all preferences.")
            plan = (self.services.plan(root, current, moves, proposal_scan_id=scan_id)
                    if reply["kind"] == "proposal" and not event.is_set() else None)
            return current, copy.deepcopy(reply), plan

        def accept(value):
            if value is None:
                return
            self.snapshot, reply, plan = value
            self.history.append({"request": request, "reply": reply})
            self.model_status = "Local Ollama replied"
            self._message("Local model (suggestion)", reply["message"])
            if plan is not None and plan.operations:
                self._review(plan, {"moves": reply["moves"]},
                             tuple(move["file_id"] for move in reply["moves"]))
            else:
                self.state = "clarification" if reply["kind"] == "clarification" else "ready"
                if reply["kind"] == "proposal":
                    self._message("Application", "No moves proposed. Nothing to apply.")

        def failure(error):
            self.model_status = "Request failed / reply rejected"
            self.state = "blocked"
            self._message("Application", f"Request blocked: {error}")

        result = self._start(work, accept, failure=failure)
        self.cancel_event = event
        return result

    def _review(self, plan, proposal, selected):
        self.plan, self.proposal = plan, proposal
        card = ReviewCard(plan, proposal, tuple(selected))
        self.cards.append(card)
        self.timeline.append(("card", card))
        self.state = "reviewable"

    def select_moves(self, plan_id, selected):
        if (self.busy or self.closing or self.plan is None or self.plan.plan_id != plan_id
                or self.state not in ("reviewable", "confirming")):
            return False
        root, snapshot, proposal = self.root, self.snapshot, copy.deepcopy(self.proposal)
        scan_id = snapshot.scan_id
        selected = tuple(selected)
        self._invalidate()
        self.state = "thinking"
        return self._start(
            lambda: self.services.plan(root, snapshot, proposal, proposal_scan_id=scan_id,
                                       selected_file_ids=selected),
            lambda plan: self._review(plan, proposal, selected),
        )

    def open_confirmation(self, plan_id):
        if (self.busy or self.closing or self.recovery_blocked or self.state != "reviewable"
                or self.plan is None or self.plan.plan_id != plan_id or not self.plan.operations):
            return False
        self.confirmation = self.plan
        self.state = "confirming"
        self._changed()
        return True

    def cancel_confirmation(self):
        if self.state == "confirming":
            self.confirmation = None
            self.state = "reviewable"
            self._changed()

    def confirm_moves(self, plan_id):
        plan = self.confirmation
        if (self.busy or self.closing or self.recovery_blocked or self.state != "confirming"
                or plan is None or plan is not self.plan or plan.plan_id != plan_id
                or self.snapshot is None or plan.scan_id != self.snapshot.scan_id or plan.root != self.root):
            return False
        root, scan_id = self.root, self.snapshot.scan_id
        try:
            approval = self.services.approve(plan, workspace=root, scan_id=scan_id, confirmed=True)
        except Exception as exc:
            self._invalidate()
            self.state = "blocked"
            self._message("Application", f"Approval rejected: {exc}")
            self._changed()
            return False
        self.confirmation = None
        self.state = "applying"
        for card in self.cards:
            if card.plan is plan:
                card.status = "Submitted"
        event = threading.Event()

        def work():
            return self.services.execute(root, plan, scan_id=scan_id, approval=approval, cancel_event=event)

        def accept(report):
            self.report, self.result_plan = report, plan
            self._invalidate(clear_scan=True)
            self.state = "blocked" if report.recovery_required else "finished"
            self.recovery_blocked = report.recovery_required
            self._message("Application result", format_report(report, plan))

        def failure(error):
            self._invalidate(clear_scan=True)
            self.recovery_blocked = True
            self.state = "blocked"
            self._message("Application", f"Execution outcome could not be obtained: {error}. "
                          "Inspect recovery before further work. Records are retained; no automatic retry.")

        result = self._start(work, accept, failure=failure)
        self.cancel_event = event
        return result

    def open_recovery_confirmation(self):
        if self.busy or self.closing or self.recovery_review is None:
            return False
        self.recovery_confirmation = self.recovery_review
        self._changed()
        return True

    def cancel_recovery_confirmation(self):
        self.recovery_confirmation = None
        self._changed()

    def confirm_recovery(self, review=None):
        if (self.busy or self.closing or review is None or review is not self.recovery_confirmation
                or review is not self.recovery_review):
            return False
        root = self.root
        self._invalidate(clear_scan=True)
        self.recovery_review = None
        self.state = "thinking"

        def work():
            self.services.acknowledge(root, review)
            return self.services.inspect(root)

        return self._start(work, self._accept_recovery)

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            if self.state != "applying":
                self._invalidate()
                self._pending = None
                self.state = "blocked" if self.recovery_blocked else ("ready" if self.root else "no_folder")
                self._message("Application", "Cancelled. Waiting for the occupied worker; late replies will be discarded.")
            else:
                self._message("Application", "Stop requested. Waiting for the current operation and actual outcome.")
            self._changed()

    def close(self):
        self.closing = True
        self._pending = None
        if self.busy:
            self.cancel()
        else:
            self.closed = True
        self._changed()
