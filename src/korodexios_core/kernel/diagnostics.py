"""Structured module diagnostics: severity, message, remediation hint.

A module knows what an operator should check; the chassis only knows how to
collect and render it. Modules contribute DiagnosticCheck entries through the
optional diagnostics() hook and they land in 'kdx doctor' and 'kdx status'.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "DiagnosticCheck",
    "DiagnosticSeverity",
    "aggregate_diagnostics",
    "diagnostic_check",
]


class DiagnosticSeverity(StrEnum):
    """How much attention a diagnostic finding deserves."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def is_problem(self) -> bool:
        """Return True when the finding should fail a health check."""
        return self is not DiagnosticSeverity.INFO


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One named check contributed by a module or by the chassis itself."""

    name: str
    message: str = ""
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    hint: str = ""
    module: str = ""

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("a diagnostic check needs a name")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(
            self,
            "severity",
            DiagnosticSeverity(str(self.severity).strip().lower()),
        )

    @property
    def is_problem(self) -> bool:
        """Return True when this finding is a warning or an error."""
        return self.severity.is_problem

    def describe(self) -> str:
        """Return the one-line rendering used by 'kdx doctor'."""
        owner = f"{self.module}/{self.name}" if self.module else self.name
        text = f"{owner}: {self.message}" if self.message else owner
        return f"{text} ({self.severity.value})"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the check."""
        return {
            "name": self.name,
            "message": self.message,
            "severity": str(self.severity.value),
            "hint": self.hint,
            "module": self.module,
        }


def diagnostic_check(
    name: str,
    message: str = "",
    *,
    severity: DiagnosticSeverity | str = DiagnosticSeverity.INFO,
    hint: str = "",
    module: str = "",
) -> DiagnosticCheck:
    """Return one DiagnosticCheck; the terse spelling modules reach for."""
    return DiagnosticCheck(
        name=name,
        message=message,
        severity=DiagnosticSeverity(str(severity).strip().lower()),
        hint=hint,
        module=module,
    )


def aggregate_diagnostics(checks: Iterable[DiagnosticCheck]) -> dict[str, Any]:
    """Return the report block 'kdx doctor' renders for module diagnostics."""
    collected = list(checks)
    errors = sum(1 for item in collected if item.severity is DiagnosticSeverity.ERROR)
    warnings = sum(1 for item in collected if item.severity is DiagnosticSeverity.WARNING)
    info = sum(1 for item in collected if item.severity is DiagnosticSeverity.INFO)
    return {
        "total": len(collected),
        "ok": errors == 0 and warnings == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "checks": [item.as_dict() for item in collected],
    }
