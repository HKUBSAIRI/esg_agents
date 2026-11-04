"""Operational events are separate from A public records and never carry full prompts."""
from contextlib import contextmanager
from contextvars import ContextVar
from .common import utcnow

_SINK = ContextVar("b_audit_events", default=None)


def emit(event, *, run_id, pid, level="INFO", error_code=None, **context):
    sink = _SINK.get()
    if sink is not None:
        item = {"ts":utcnow(), "level":level, "event":event, "stage":"audit", "audit_run_id":run_id,
                "pid":pid, "message":event, "context":context}
        if error_code:
            item["error_code"] = error_code
        sink.append(item)


@contextmanager
def capture_events():
    events = []
    token = _SINK.set(events)
    try:
        yield events
    finally:
        _SINK.reset(token)
