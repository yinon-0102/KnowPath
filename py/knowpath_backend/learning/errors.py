"""Shared learning-domain errors, independent of transport and persistence."""


class DomainNotFound(Exception):
    def __init__(self, resource: str, resource_id: str):
        super().__init__(f"{resource} not found: {resource_id}")
        self.resource = resource
        self.resource_id = resource_id


class DomainConflict(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class EventHistoryExpired(DomainConflict):
    def __init__(self, run_id: str):
        super().__init__("EVENT_HISTORY_EXPIRED", f"run event history expired: {run_id}")
