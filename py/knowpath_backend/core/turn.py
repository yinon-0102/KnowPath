"""Generic turn lifecycle policies, independent of application services."""
from dataclasses import dataclass
from enum import StrEnum


class TurnCommitMode(StrEnum):
    AUTOMATIC = 'automatic'
    EXTERNAL = 'external'


class TurnFailure(RuntimeError):
    """A turn cannot produce a publishable candidate."""


class TurnCancelled(TurnFailure):
    pass


class TurnBudgetExceeded(TurnFailure):
    pass


class EmptyTurnResponse(TurnFailure):
    pass


@dataclass(frozen=True)
class TurnExecutionPolicy:
    strict: bool = False

    @classmethod
    def web(cls):
        return cls(strict=True)
