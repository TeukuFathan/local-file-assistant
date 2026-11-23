"""Read-only diagnostic window. It receives a store, never execution services."""

from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)


def diagnostic_summary(record):
    elapsed = "In progress" if record.elapsed_seconds is None else f"{record.elapsed_seconds:.3f} seconds"
    lines = [
        f"Conversation request: {record.request_id}", f"Model: {record.model}",
        f"Captured revision: {record.revision} | Scan: {record.scan_id or 'Not available'}",
        f"Elapsed request (including preparation/planning): {elapsed}",
        f"User request:\n{record.request}",
        f"Validation/network/application errors:\n{record.error or 'None recorded'}",
        "Diagnostic data may belong to a cancelled or superseded request. It is not approval or an executable plan.",
    ]
    for item in record.exchanges:
        label = "Exact JSON request sent" if item.request_sent else "Attempted JSON request (send not confirmed)"
        lines.extend([
            f"\nPOST {item.path} | HTTP status: {item.status if item.status is not None else 'Not received'}",
            f"{label}:\n{item.request_json}",
            ("Raw HTTP response (incomplete received prefix; UTF-8; invalid bytes escaped):\n"
             if item.response_incomplete else "Raw HTTP response (UTF-8; invalid bytes escaped):\n") + (
                item.response_text if item.response_text is not None else
                "No body captured: no complete read, timeout, or existing status/header checks rejected it before reading."),
        ])
    if not record.exchanges:
        lines.append("No HTTP request captured.")
    lines.extend([
        "\nRaw assistant content:\n" + (record.raw_content if record.raw_content is not None else "Not available"),
        f"\nValidated proposal (Ollama validation only; not a T6 plan):\n{record.proposal_json or 'Not available'}",
    ])
    if record.reply_kind == "clarification":
        lines.append("Validated clarification; no executable proposal.")
    if record.truncated_fields:
        lines.append("\nTRUNCATED diagnostic fields: " + ", ".join(record.truncated_fields))
    return "\n\n".join(lines)


class DeveloperDetails(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        if parent is not None:
            self.setFont(parent.font())
        self.store = store
        self._version = None
        self._records = ()
        self.setWindowTitle("Developer details")
        self.resize(820, 620)
        self.setMinimumSize(580, 360)
        layout = QVBoxLayout(self)
        guidance = QLabel("Read only · Latest four requests, bounded in memory only. Copy is manual; Clear does not change chat or approval.")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.requests = QComboBox()
        self.requests.setAccessibleName("Captured conversation request")
        self.requests.currentIndexChanged.connect(self._select)
        layout.addWidget(self.requests)
        self.text = QPlainTextEdit()
        self.text.setFont(self.font())
        self.text.setReadOnly(True)
        self.text.setAcceptDrops(False)
        self.text.setAccessibleName("Developer diagnostic details")
        layout.addWidget(self.text, 1)
        controls = QHBoxLayout()
        controls.addStretch()
        self.copy_button = QPushButton("Copy")
        self.clear_button = QPushButton("Clear")
        close_button = QPushButton("Close")
        for button in (self.copy_button, self.clear_button, close_button):
            button.setAutoDefault(False)
            button.setDefault(False)
            controls.addWidget(button)
        self.copy_button.clicked.connect(self._copy)
        self.clear_button.clicked.connect(self._clear)
        close_button.clicked.connect(self.close)
        layout.addLayout(controls)
        self.refresh(force=True)

    def refresh(self, *, force=False):
        try:
            version = self.store.version
            if version == self._version and not force:
                return
            selected = self.requests.currentData()
            self._records = self.store.snapshot()
            self._version = version
            self.requests.blockSignals(True)
            try:
                self.requests.clear()
                for record in self._records:
                    self.requests.addItem(f"{record.request_id} · {record.model} · {record.request[:60]}", record.request_id)
                index = self.requests.findData(selected)
                self.requests.setCurrentIndex(index if index >= 0 else len(self._records) - 1)
            finally:
                self.requests.blockSignals(False)
            self._select()
        except Exception:
            self._records = ()
            self.text.setPlainText("Developer details unavailable.")
            self.copy_button.setEnabled(False)

    def _select(self, index=None):
        try:
            selected = self.requests.currentData()
            record = next((item for item in self._records if item.request_id == selected), None)
            self.text.setPlainText(diagnostic_summary(record) if record else "")
            self.copy_button.setEnabled(record is not None)
            self.clear_button.setEnabled(bool(self._records))
        except Exception:
            self.text.setPlainText("Developer details unavailable.")
            self.copy_button.setEnabled(False)

    def _copy(self):
        try:
            QApplication.clipboard().setText(self.text.toPlainText())
        except Exception:
            pass

    def _clear(self):
        try:
            self.store.clear()
            self.refresh(force=True)
        except Exception:
            pass

    def hideEvent(self, event):
        try:
            self._records = ()
            self._version = None
            self.requests.clear()
            self.text.clear()
            self.copy_button.setEnabled(False)
        except Exception:
            pass
        super().hideEvent(event)
