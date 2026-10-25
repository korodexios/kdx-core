"""Error hierarchy. Every chassis error carries a polite message and an exit code."""

from __future__ import annotations

from typing import ClassVar

from .constants import TAG_ERROR, ExitCode


class KorodexiosError(Exception):
    """Base class for expected failures; never renders a Python traceback."""

    exit_code: ClassVar[int] = int(ExitCode.ERROR)
    default_hint: ClassVar[str | None] = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint if hint is not None else type(self).default_hint

    def render(self) -> str:
        """Return the operator-facing rendering shown on stderr."""
        lines = [f"{TAG_ERROR} {self.message}"]
        if self.hint:
            lines.append(f"    hint: {self.hint}")
        return "\n".join(lines)


class ConfigurationError(KorodexiosError):
    """Configuration could not be loaded, parsed, or coerced."""

    exit_code: ClassVar[int] = int(ExitCode.CONFIG_ERROR)
    default_hint: ClassVar[str | None] = "Check the key path, the config file, and CLI overrides."


class ConfigFileError(ConfigurationError):
    """A config or .env file is missing, unreadable, or malformed."""


class ConfigValueError(ConfigurationError):
    """A resolved value does not match the type declared by its consumer."""

    def __init__(
        self,
        message: str,
        *,
        key: str,
        value: object,
        source: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.key = key
        self.value = value
        self.source = source

    def render(self) -> str:
        """Append the offending key, value, and provenance to the base rendering."""
        return f"{super().render()}\n    key: {self.key} = {self.value!r} (from {self.source})"


class PathDiscoveryError(ConfigurationError):
    """The project root or one of its standard directories is unusable."""


class ModuleContractError(KorodexiosError):
    """A candidate object does not satisfy the module contract."""


class ModuleDiscoveryError(KorodexiosError):
    """A module could not be imported or resolved from disk."""

    def __init__(
        self,
        message: str,
        *,
        module_name: str,
        stage: str = "import",
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.module_name = module_name
        self.stage = stage


class ModuleLifecycleError(KorodexiosError):
    """A module raised while the chassis drove one of its lifecycle hooks."""

    def __init__(
        self,
        message: str,
        *,
        module_name: str,
        phase: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.module_name = module_name
        self.phase = phase


class ChassisError(KorodexiosError):
    """The chassis itself reached an unusable state."""


class InstanceLockError(ChassisError):
    """Another live chassis already owns this root directory."""

    default_hint = "Stop the running daemon first, or run 'kdx status' to see it."


class LockTimeoutError(ChassisError):
    """A named lock stayed busy for the whole time the caller was willing to wait."""

    default_hint = "Shorten the critical section, raise the timeout, or run 'kdx doctor'."

    def __init__(
        self,
        message: str,
        *,
        name: str = "",
        timeout: float | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.name = name
        self.timeout = timeout

    def render(self) -> str:
        """Append the lock name and the waited time to the base rendering."""
        base = super().render()
        if not self.name:
            return base
        waited = "without waiting" if self.timeout in (None, 0.0) else f"after {self.timeout}s"
        return f"{base}\n    lock: {self.name} ({waited})"


class CliError(KorodexiosError):
    """The command line was used incorrectly or a command failed politely."""

    exit_code: ClassVar[int] = int(ExitCode.USAGE_ERROR)
