from __future__ import annotations


class AgentUnavailableError(Exception):
    """Raised when the agent runtime cannot run because LLM is required but unavailable."""

    status_code: int = 503

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentTurnError(Exception):
    """Raised when an agent turn fails due to runtime or upstream errors."""

    status_code: int = 502

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMIncompleteResponseError(AgentTurnError):
    """A completed generation contained no final answer or executable tool call."""


class AttachmentInputError(AgentTurnError):
    """An attachment cannot be supplied as actual model evidence."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)


class LLMContextBudgetError(AgentTurnError):
    """The request cannot fit the loaded model without losing supplied evidence."""


class TurnInterruptedError(Exception):
    """Raised when a turn is cancelled via the turn control registry."""

    def __init__(self, partial: str = "") -> None:
        self.partial = partial
        super().__init__("turn interrupted")
