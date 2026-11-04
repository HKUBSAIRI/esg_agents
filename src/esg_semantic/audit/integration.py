"""Internal dependency binding, not a second set of A public data classes."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from copy import deepcopy


@dataclass(frozen=True)
class Integration:
    fixture: bool = False
    validator: object = None
    outcome_factory: object = None
    catalogue: dict = field(default_factory=dict)
    visual_reader: object = None
    renderer: object = None
    contract_fingerprint: str | None = None
    validation_scope: str = "formal"
    record_format: str = "indicator"
    execution_mode: str = "model"


_ACTIVE = ContextVar("b_audit_integration", default=Integration())


def current():
    return _ACTIVE.get()


@contextmanager
def integration_context(**kwargs):
    """Configure A validation/client boundary at the application edge, not inside A's models."""
    binding = Integration(**kwargs)
    if binding.validation_scope not in ("formal", "local_delivery") or binding.record_format not in ("indicator", "profile_delivery_v1") or binding.execution_mode not in ("model", "rules_only"):
        raise ValueError("Unsupported audit binding mode")
    if binding.record_format == "profile_delivery_v1" and binding.validation_scope != "local_delivery":
        raise ValueError("Delivery projection cannot certify A formal contracts")
    if not binding.fixture and (not callable(binding.validator) or not binding.contract_fingerprint):
        raise ValueError("Formal mode requires A validator and contract fingerprint")
    token = _ACTIVE.set(binding)
    try:
        yield binding
    finally:
        _ACTIVE.reset(token)


def validate_as(name, value):
    binding = current()
    if callable(binding.validator):
        binding.validator(name, deepcopy(value))


def finish(outcome):
    validate_as("AuditOutcome", outcome)
    factory = current().outcome_factory
    return factory(deepcopy(outcome)) if callable(factory) else outcome
