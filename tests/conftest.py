"""Shared fixtures and the scripted module double used across the suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from korodexios_core.config.layered import LayeredConfig
from korodexios_core.kernel.cli_contract import CliCommand
from korodexios_core.paths import AppPaths

PROJECT_CONFIG = """
core:
  log:
    level: INFO
    file_enabled: false
  modules:
    autodiscover: true
    extra_paths: []
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
modules:
  scripted:
    enabled: true
"""


class ScriptedModule:
    """Contract-satisfying test double that records every lifecycle call."""

    def __init__(
        self,
        name: str = "scripted",
        *,
        fail_phase: str | None = None,
        stop_delay: float = 0.0,
        priority: int = 100,
    ) -> None:
        self.name = name
        self.description = f"Scripted module {name}."
        self.version = "0.1.0"
        self.priority = priority
        self.calls: list[str] = []
        self.commands: list[str] = []
        self.context: Any = None
        self._fail_phase = fail_phase
        self._stop_delay = stop_delay

    def init(self, context: Any) -> None:
        self.calls.append("init")
        self.context = context
        self._maybe_raise("init")

    def register_cli(self, surface: Any) -> None:
        self.calls.append("register_cli")
        self.commands.append(self.name)
        surface.add_command(
            CliCommand(name=self.name, help="Scripted command.", handler=_noop_handler)
        )

    def start(self) -> None:
        self.calls.append("start")
        self._maybe_raise("start")

    def stop(self) -> None:
        self.calls.append("stop")
        if self._stop_delay:
            import time

            time.sleep(self._stop_delay)
        self._maybe_raise("stop")

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "calls": list(self.calls)}

    def _maybe_raise(self, phase: str) -> None:
        if self._fail_phase == phase:
            raise RuntimeError(f"{self.name} exploded during {phase}")


def _noop_handler(request: Any) -> int:
    return 0


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Return a throwaway chassis root with a minimal kdx.yml."""
    (tmp_path / "kdx.yml").write_text(PROJECT_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def paths(project_root: Path) -> AppPaths:
    """Return resolved paths for the throwaway project."""
    return AppPaths.discover(root=str(project_root), environ={})


@pytest.fixture()
def config(project_root: Path) -> LayeredConfig:
    """Return a layered configuration loaded from the throwaway project."""
    resolved = AppPaths.discover(root=str(project_root), environ={})
    return LayeredConfig.load(
        config_file=resolved.config_file,
        env_file=resolved.env_file,
        environ={"KORODEXIOS_CORE__LOG__LEVEL": "DEBUG"},
    )


@pytest.fixture()
def scripted() -> ScriptedModule:
    """Return a fresh scripted module double."""
    return ScriptedModule()
