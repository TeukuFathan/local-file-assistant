"""Qt Widgets view; the conversation controller owns all workflow decisions."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .formatting import plan_summary, recovery_summary
from .developer_details import DeveloperDetails
from .theme import apply_theme


class Composer(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class DesktopApp(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.developer_details = None
        self.setObjectName("app")
        self.font_family = apply_theme(self)
        self.setWindowTitle("Local File Assistant")
        self.resize(1080, 850)
        self.setMinimumSize(820, 650)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 16)
        root.setSpacing(12)
        root.addWidget(self._label("Local File Assistant", "title"))
        root.addWidget(self._label("Organize by filename. You stay in control.", "muted"))

        context = QFrame()
        context.setProperty("role", "panel")
        context_layout = QVBoxLayout(context)
        context_layout.setContentsMargins(18, 16, 18, 16)
        header = QHBoxLayout()
        self.folder = self._label("No folder selected")
        self.folder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.folder, 1)
        self.choose = self._button("Change folder", self._choose)
        header.addWidget(self.choose)
        self.developer_button = self._button("Developer details", self._show_developer_details)
        header.addWidget(self.developer_button)
        context_layout.addLayout(header)
        controls = QHBoxLayout()
        controls.addWidget(self._label("Local model", "muted"))
        self.model = QLineEdit()
        self.model.setText(controller.model or "qwen3:4b")
        self.model.setPlaceholderText("Installed model name")
        self.model.setAccessibleName("Installed local Ollama model")
        self.model.setMinimumWidth(140)
        controls.addWidget(self.model, 1)
        self.model_button = self._button("Select model", lambda: self.c.select_model(self.model.text()), "soft")
        self.scan = self._button("Inspect / rescan", self.c.rescan)
        self.stop = self._button("Stop", self.c.cancel)
        for button in (self.model_button, self.scan, self.stop):
            controls.addWidget(button)
        context_layout.addLayout(controls)
        root.addWidget(context)
        self.status = self._label("", "muted")
        root.addWidget(self.status)
        self.recovery_button = self._button("Review recovery evidence", self.c.open_recovery_confirmation, "soft")
        root.addWidget(self.recovery_button)

        self.scroll = QScrollArea()
        self.scroll.viewport().setObjectName("conversationViewport")
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("conversationBody")
        self.body.setAutoFillBackground(False)
        self.messages = QVBoxLayout(self.body)
        self.messages.setContentsMargins(0, 0, 8, 0)
        self.messages.setSpacing(12)
        self.messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        composer = QFrame()
        composer.setProperty("role", "panel")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(14, 10, 14, 10)
        self.input = Composer()
        self.input.setObjectName("composer")
        self.input.setAccessibleName("Organization request")
        self.input.setPlaceholderText("Describe how you would like these files organized…")
        self.input.setAcceptDrops(False)
        self.input.setFixedHeight(62)
        self.input.submitted.connect(self._send)
        composer_layout.addWidget(self.input, 1)
        self.send_button = self._button("Send", self._send, "primary")
        composer_layout.addWidget(self.send_button)
        root.addWidget(composer)
        root.addWidget(self._label("Filenames only · Enter to send · Shift+Enter for a new line · Review before moving", "footer"))

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_end)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._schedule_scroll)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(50)
        self.c.on_change = self.render
        self.render()

    @staticmethod
    def _label(text, role=""):
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setProperty("role", role)
        return label

    @staticmethod
    def _button(text, callback, kind="quiet"):
        button = QPushButton(text)
        button.setProperty("kind", kind)
        # Enter in the composer sends text. No button is a default action.
        button.setAutoDefault(False)
        button.setDefault(False)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def _choose(self):
        picked = QFileDialog.getExistingDirectory(self, "Choose the folder to organize")
        if picked:
            self.c.select_root(Path(picked))

    def _send(self):
        if self.c.send(self.input.toPlainText()):
            self.input.clear()

    def _show_developer_details(self):
        try:
            if self.developer_details is None:
                self.developer_details = DeveloperDetails(self.c.services.diagnostics, self)
            self.developer_details.refresh(force=True)
            self.developer_details.show()
            self.developer_details.raise_()
        except Exception:
            pass

    def _panel(self, title, *, user=False):
        panel = QFrame()
        panel.setProperty("role", "user" if user else "panel")
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        layout.addWidget(self._label(title, "heading"))
        self.messages.addWidget(panel)
        return layout

    def _text(self, layout, text):
        # QPlainTextEdit never interprets model text or paths as HTML/links.
        widget = QPlainTextEdit()
        widget.setFont(self.font())
        widget.setReadOnly(True)
        widget.setPlainText(text)
        widget.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        widget.setFixedHeight(min(12, max(2, text.count("\n") + len(text) // 100 + 1)) * widget.fontMetrics().lineSpacing() + 12)
        layout.addWidget(widget)

    def _card(self, card):
        c = self.c
        layout = self._panel(f"MOVE REVIEW · {card.status}")
        self._text(layout, plan_summary(card.plan))
        active = (card.status == "Current" and c.plan is card.plan and not c.busy
                  and not c.closing and c.state in ("reviewable", "confirming"))
        if active and c.snapshot:
            files = {file.file_id: file.filename for file in c.snapshot.files}
            existing = {folder.casefold() for folder in c.snapshot.folders}
            paths = {op.request.source: op.request.destination for op in card.plan.operations if op.request.action == "move"}
            checks = {}
            for move in card.proposal["moves"]:
                identifier, folder = move["file_id"], move["destination_folder"]
                source = files[identifier]
                destination = paths.get(source, f"{folder}/{source}")
                row = QHBoxLayout()
                check = QCheckBox()
                check.setAccessibleName(f"Select move {source} to {destination}")
                check.setChecked(identifier in card.selected)
                checks[identifier] = check
                row.addWidget(check)
                status = "already exists" if folder.casefold() in existing else "new; created only if selected and confirmed"
                self._text(row, f"{source} -> {destination}\nFolder: {status}")
                layout.addLayout(row)
                check.toggled.connect(lambda _checked, plan_id=card.plan.plan_id, selection=checks: c.select_moves(
                    plan_id, tuple(key for key, value in selection.items() if value.isChecked())))
        button = self._button("Review and apply", lambda: c.open_confirmation(card.plan.plan_id), "primary")
        button.setEnabled(active and bool(card.plan.operations) and c.state == "reviewable")
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignRight)

    def _actions(self, layout, title, confirm, cancel):
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self._button("Cancel", cancel))
        button = self._button(title, confirm, "primary")
        actions.addWidget(button)
        layout.addLayout(actions)

    def render(self):
        c = self.c
        self.folder.setText(f"Folder: {c.root}" if c.root else "No folder selected")
        self.status.setText(f"{c.state.replace('_', ' ').capitalize()} | Model: {c.model or 'none selected'} | "
                            f"Ollama: {c.model_status}" + (" | Closing: waiting for worker outcome" if c.closing else ""))
        frozen = c.state == "applying" or c.closing
        self.choose.setEnabled(not frozen)
        self.scan.setEnabled(not frozen and c.root is not None)
        for widget in (self.model, self.model_button):
            widget.setEnabled(not frozen and not c.busy)
        self.input.setEnabled(not frozen)
        self.send_button.setEnabled(not c.busy and not c.closing and not c.recovery_blocked)
        self.stop.setEnabled(c.busy)
        self.recovery_button.setVisible(c.recovery_review is not None)
        self.recovery_button.setEnabled(not c.busy and not c.closing)
        while self.messages.count():
            widget = self.messages.takeAt(0).widget()
            widget.setEnabled(False)
            widget.hide()
            widget.deleteLater()
        for kind, value in c.timeline:
            if kind == "message":
                role, text = value
                self._text(self._panel(role, user=role == "You"), text)
            else:
                self._card(value)
        if c.confirmation:
            plan = c.confirmation
            layout = self._panel("CONFIRM MOVES · Exact selected plan")
            self._text(layout, plan_summary(plan))
            self._actions(layout, "Confirm moves", lambda: c.confirm_moves(plan.plan_id), c.cancel_confirmation)
        if c.recovery_confirmation:
            review = c.recovery_confirmation
            layout = self._panel("RECOVERY · Separate acknowledgement")
            self._text(layout, recovery_summary(review))
            self._actions(layout, "Confirm acknowledgement", lambda: c.confirm_recovery(review), c.cancel_recovery_confirmation)
        self._schedule_scroll()

    def _schedule_scroll(self):
        self._scroll_timer.stop()
        if not self.c.closing:
            self._scroll_timer.start(0)

    def _scroll_to_end(self):
        self.messages.activate()
        scrollbar = self.scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _poll(self):
        self.c.worker.poll()
        try:
            if self.developer_details is not None and self.developer_details.isVisible():
                self.developer_details.refresh()
        except Exception:
            pass
        if self.c.closed:
            self.close()

    def closeEvent(self, event):
        self.c.close()
        if self.c.closed:
            self._poll_timer.stop()
            self._scroll_timer.stop()
            try:
                if self.developer_details is not None:
                    self.developer_details.close()
            except Exception:
                pass
            self.c.on_change = lambda: None
            event.accept()
        else:
            event.ignore()  # Keep polling; never join a worker on the GUI thread.
