"""Exception hierarchy. Each class carries the failure category used by reporting (Chapter 13)."""


class AutoplatError(Exception):
    category = "unknown"


class InfrastructureError(AutoplatError):
    """Shared services or the platform itself failed (CI agent, disk, network share)."""
    category = "infrastructure"


class TargetUnreachable(InfrastructureError):
    """The target cannot be contacted over its control link."""


class BenchError(AutoplatError):
    """Lab hardware or its configuration failed."""
    category = "bench"


class InstrumentBusy(BenchError):
    """An instrument or bench is held by another user or run."""


class CaptureNoSignal(BenchError):
    """A capture device reports no valid input signal."""


class BenchSafetyError(BenchError):
    """A requested action violates the bench's configured safety limits."""


class ToolError(AutoplatError):
    """An agent tool refused a request; the message explains how to make a valid one."""
    category = "test"
