"""autoplat: evidence-driven automotive bench automation (companion to the book)."""
from autoplat.errors import (
    AutoplatError,
    BenchError,
    BenchSafetyError,
    CaptureNoSignal,
    InfrastructureError,
    InstrumentBusy,
    TargetUnreachable,
    ToolError,
)
from autoplat.waiting import WaitTimeout, wait_until, with_retry

__all__ = [
    "AutoplatError", "BenchError", "BenchSafetyError", "CaptureNoSignal",
    "InfrastructureError", "InstrumentBusy", "TargetUnreachable", "ToolError",
    "WaitTimeout", "wait_until", "with_retry",
]
__version__ = "1.0.0"
