"""Bounded, process-local observations. Nothing here authorizes an operation."""

import json
import time
from dataclasses import dataclass, replace
from threading import Lock


MAX_RECORDS = 4
MAX_REQUEST_TEXT_BYTES = 512 * 1024
MAX_RESPONSE_TEXT_BYTES = 256 * 1024
MAX_DETAIL_BYTES = 512 * 1024
MAX_ERROR_BYTES = 4 * 1024


def emit(observer, event, **fields):
    """Diagnostic errors must never alter transport, validation or UI behavior."""
    if observer is not None:
        try:
            observer(event, **fields)
        except Exception:
            pass


def emit_json(observer, event, value, **fields):
    if observer is not None:
        try:
            # Observers only see immutable text, never a live validated object.
            emit(observer, event, text=json.dumps(value, ensure_ascii=True, indent=2), **fields)
        except Exception:
            pass


def report_error(observer, error):
    if observer is not None:
        try:
            parts = []
            seen = set()
            while error is not None and id(error) not in seen and len(parts) < 4:
                seen.add(id(error))
                parts.append(f"{type(error).__name__}: {error}")
                error = error.__cause__
            emit(observer, "error", text="\nCaused by: ".join(parts))
        except Exception:
            pass


def _clip(value, limit):
    if isinstance(value, bytes):
        text = value[:limit].decode("utf-8", errors="backslashreplace")
    else:
        text = value[:limit]
    encoded = text.encode("utf-8", errors="backslashreplace")
    truncated = len(value) > limit or len(encoded) > limit
    return encoded[:limit].decode("utf-8", errors="ignore"), truncated


@dataclass(frozen=True)
class HttpExchange:
    path: str
    request_json: str
    request_sent: bool = False
    status: int | None = None
    response_text: str | None = None
    response_incomplete: bool = False


@dataclass(frozen=True)
class DiagnosticRecord:
    request_id: str
    model: str
    request: str
    revision: int | None
    scan_id: str | None
    exchanges: tuple[HttpExchange, ...] = ()
    raw_content: str | None = None
    proposal_json: str | None = None
    reply_kind: str | None = None
    error: str | None = None
    elapsed_seconds: float | None = None
    truncated_fields: tuple[str, ...] = ()


class DiagnosticStore:
    """Latest four requests; all fields and the two HTTP exchanges are bounded.

    Clear/eviction removes the record permanently, even if its worker is still
    running. Snapshots are immutable and contain no controller/backend objects.
    """

    def __init__(self):
        self._lock = Lock()
        self._records = ()
        self._counter = self._version = 0

    @property
    def version(self):
        with self._lock:
            return self._version

    def begin(self, request, model, *, revision=None, scan_id=None):
        request, request_cut = _clip(request, 8 * 1024)
        model, model_cut = _clip(model, 128)
        scan_id, scan_cut = _clip(scan_id, 128) if scan_id is not None else (None, False)
        with self._lock:
            self._counter += 1
            request_id = f"request-{self._counter}"
            record = DiagnosticRecord(request_id, model, request, revision, scan_id,
                                      truncated_fields=tuple(name for name, cut in (
                                          ("user request", request_cut), ("model", model_cut),
                                          ("scan identity", scan_cut)) if cut))
            self._records = (*self._records, record)[-MAX_RECORDS:]
            self._version += 1
        return _Capture(self, request_id)

    def snapshot(self):
        with self._lock:
            return self._records

    def clear(self):
        with self._lock:
            self._records = ()
            self._version += 1

    def _event(self, request_id, event, fields):
        with self._lock:
            index = next((i for i, record in enumerate(self._records) if record.request_id == request_id), None)
            if index is None:
                return
            record = self._records[index]
            truncated = list(record.truncated_fields)

            def clip(value, limit, label):
                text, cut = _clip(value, limit)
                if cut and label not in truncated:
                    truncated.append(label)
                return text

            if event in {"request", "sent", "status", "response"}:
                path = fields["path"]
                if path not in ("/api/show", "/api/chat"):
                    return
                exchanges = list(record.exchanges)
                position = next((i for i, item in enumerate(exchanges) if item.path == path), None)
                if event == "request":
                    item = HttpExchange(path, clip(fields["payload"], MAX_REQUEST_TEXT_BYTES, path + " request"))
                    if position is None:
                        position = len(exchanges)
                        exchanges.append(item)
                    else:
                        exchanges[position] = item
                elif position is not None:
                    item = exchanges[position]
                    if event == "sent":
                        item = replace(item, request_sent=True)
                    elif event == "status":
                        item = replace(item, status=fields["status"])
                    else:
                        item = replace(item, response_text=clip(fields["body"], MAX_RESPONSE_TEXT_BYTES, path + " response"),
                                       response_incomplete=fields.get("incomplete", False))
                    exchanges[position] = item
                record = replace(record, exchanges=tuple(exchanges))
            elif event == "content":
                record = replace(record, raw_content=clip(fields["text"], MAX_RESPONSE_TEXT_BYTES, "assistant content"))
            elif event == "binding":
                record = replace(record, scan_id=clip(fields["scan_id"], 128, "scan identity"))
            elif event == "validated":
                record = replace(record, reply_kind=fields["kind"],
                                 proposal_json=(clip(fields["text"], MAX_DETAIL_BYTES, "validated proposal")
                                                if fields["kind"] == "proposal" else None))
            elif event == "error":
                record = replace(record, error=clip(fields["text"], MAX_ERROR_BYTES, "error"))
            elif event == "finished":
                record = replace(record, elapsed_seconds=fields["elapsed_seconds"])
            else:
                return
            record = replace(record, truncated_fields=tuple(truncated))
            self._records = (*self._records[:index], record, *self._records[index + 1:])
            self._version += 1


class _Capture:
    def __init__(self, store, request_id):
        self._store = store
        self.request_id = request_id
        self._started = time.perf_counter()

    def __call__(self, event, **fields):
        if event == "finished":
            fields = {"elapsed_seconds": max(0.0, time.perf_counter() - self._started)}
        self._store._event(self.request_id, event, fields)
