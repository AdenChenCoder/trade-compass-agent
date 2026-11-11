"""Capture exact runtime tool returns within one specialist invocation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ReceiptCapture:
    receipts: list[dict[str, str]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    closed: bool = False


_capture: ContextVar[ReceiptCapture | None] = ContextVar("tool_receipts", default=None)


@contextmanager
def capture_receipts():
    capture = ReceiptCapture()
    token = _capture.set(capture)
    try:
        yield capture.receipts
    finally:
        with capture.lock:
            capture.closed = True
        _capture.reset(token)


def record_receipt(name: str, result: str) -> str:
    capture = _capture.get()
    if capture is not None:
        with capture.lock:
            if not capture.closed:
                capture.receipts.append({"tool": name, "result": result})
    return result


def dispatched_receipts(payload):
    # Only the dispatcher-owned envelope is authoritative; never parse model output.
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        for receipt in item.get("tool_receipts", []):
            if (isinstance(receipt, dict) and isinstance(receipt.get("tool"), str)
                    and isinstance(receipt.get("result"), str)):
                yield receipt["tool"], receipt["result"]
