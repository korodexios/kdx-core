"""Contract validation and registry lifecycle tests."""

from __future__ import annotations

import pytest

from korodexios_core.exceptions import ModuleContractError
from korodexios_core.kernel.contract import (
    Module,
    ModuleProtocol,
    ModuleSpec,
    ModuleState,
    check_contract,
)
from korodexios_core.kernel.registry import ModuleRegistry, call_with_timeout

from .conftest import ScriptedModule


class DummyLogger:
    def __getattr__(self, name: str):
        def _emit(*args: object, **kwargs: object) -> None:
            return None

        return _emit


def make_spec(name: str = "scripted", **overrides: object) -> ModuleSpec:
    data = {
        "name": name,
        "description": f"{name} module.",
        "version": "0.1.0",
        "priority": 100,
        "origin": "test",
    }
    data.update(overrides)
    return ModuleSpec(**data)  # type: ignore[arg-type]


def test_check_contract_reports_violations() -> None:
    class Broken:
        name = "Broken"
        description = "no hooks"
        version = "v1"

    violations = check_contract(Broken())
    assert "missing 'init'" in violations
    assert "missing 'register_cli'" in violations
    assert any("name" in item for item in violations)
    assert any("version" in item for item in violations)


def test_module_base_class_satisfies_protocol() -> None:
    class Sample(Module):
        name = "sample"
        description = "Sample module."
        version = "0.1.0"

    sample = Sample()
    assert isinstance(sample, ModuleProtocol)
    assert sample.state is ModuleState.NEW
    with pytest.raises(ModuleContractError):
        sample.config


def test_module_base_requires_identity() -> None:
    class Nameless(Module):
        name = ""
        description = "Missing a name."
        version = "0.1.0"

    with pytest.raises(ModuleContractError):
        Nameless()


def test_registry_rejects_duplicate_names() -> None:
    registry = ModuleRegistry(DummyLogger())
    registry.register(ScriptedModule("alpha"), make_spec("alpha"))
    with pytest.raises(ModuleContractError):
        registry.register(ScriptedModule("alpha"), make_spec("alpha"))


def test_lifecycle_order_and_states() -> None:
    registry = ModuleRegistry(DummyLogger())
    first = ScriptedModule("first", priority=10)
    second = ScriptedModule("second", priority=20)
    registry.register(second, make_spec("second", priority=20))
    registry.register(first, make_spec("first", priority=10))

    assert registry.names() == ["first", "second"]
    registry.initialize_all(object())
    registry.start_all()
    assert first.calls == ["init", "start"]
    assert [record.state for record in registry.ordered()] == [
        ModuleState.RUNNING,
        ModuleState.RUNNING,
    ]
    registry.stop_all(timeout=1.0)
    assert first.calls == ["init", "start", "stop"]
    assert registry.counts()["stopped"] == 2


def test_failing_module_is_isolated() -> None:
    registry = ModuleRegistry(DummyLogger())
    healthy = ScriptedModule("healthy")
    broken = ScriptedModule("broken", fail_phase="init")
    registry.register(healthy, make_spec("healthy"))
    registry.register(broken, make_spec("broken"))
    registry.initialize_all(object())
    assert registry.get("healthy").state is ModuleState.INITIALIZED
    assert registry.get("broken").state is ModuleState.FAILED
    assert "broken" in registry.get("broken").error
    registry.start_all()
    assert registry.get("healthy").state is ModuleState.RUNNING
    registry.stop_all(timeout=1.0)
    assert "stop" in healthy.calls


def test_stop_timeout_marks_failure() -> None:
    registry = ModuleRegistry(DummyLogger())
    slow = ScriptedModule("slow", stop_delay=0.5)
    registry.register(slow, make_spec("slow"))
    registry.initialize_all(object())
    registry.start_all()
    registry.stop_all(timeout=0.05)
    record = registry.get("slow")
    assert record.state is ModuleState.FAILED
    assert "did not finish" in (record.error or "")


def test_collect_headless_populates_commands_without_a_surface() -> None:
    """The kernel collects the command tree itself, so an embedded daemon can dispatch."""
    registry = ModuleRegistry(DummyLogger())
    module = ScriptedModule("scripted")
    registry.register(module, make_spec("scripted"))
    assert registry.commands() == []
    collected = registry.collect_headless()
    assert [command.name for command in collected] == ["scripted"]
    assert [command.name for command in registry.commands()] == ["scripted"]


def test_register_cli_runs_once_across_surfaces() -> None:
    """A second surface replays recorded commands; the hook is never asked twice."""
    registry = ModuleRegistry(DummyLogger())
    module = ScriptedModule("counter")
    registry.register(module, make_spec("counter"))

    class Surface:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add_command(self, command: object) -> None:
            self.added.append(command)

    first = registry.collect_headless()
    surface = Surface()
    second = registry.collect_cli(surface)
    assert [command.name for command in first] == ["counter"]
    assert [command.name for command in second] == ["counter"]
    assert surface.added == first  # the very same command objects, replayed
    assert module.calls.count("register_cli") == 1


def test_collect_cli_records_commands() -> None:
    registry = ModuleRegistry(DummyLogger())
    module = ScriptedModule("scripted")
    registry.register(module, make_spec("scripted"))

    class Surface:
        def __init__(self) -> None:
            self.added: list[str] = []

        def add_command(self, command: object) -> None:
            self.added.append(str(command.name))

    surface = Surface()
    collected = registry.collect_cli(surface)
    assert surface.added == ["scripted"]
    assert [command.name for command in collected] == ["scripted"]
    assert registry.get("scripted").commands[0].name == "scripted"


def test_call_with_timeout_paths() -> None:
    completed, error = call_with_timeout(lambda: None, None, description="fast")
    assert completed and error is None

    def _raise() -> None:
        raise RuntimeError("nope")

    completed, error = call_with_timeout(_raise, None, description="raising")
    assert not completed and isinstance(error, RuntimeError)

    completed, error = call_with_timeout(lambda: None, 0.05, description="never")
    assert completed and error is None


def make_dependency_module(name: str, depends_on: list[str]) -> ScriptedModule:
    module = ScriptedModule(name)
    module.depends_on = tuple(depends_on)
    return module


def test_dependencies_start_before_dependents() -> None:
    registry = ModuleRegistry(DummyLogger())
    registry.register(
        make_dependency_module("gateway", ["api"]),
        make_spec("gateway", priority=10, depends_on=("api",)),
    )
    registry.register(
        make_dependency_module("api", ["database"]),
        make_spec("api", priority=30, depends_on=("database",)),
    )
    registry.register(
        make_dependency_module("database", []),
        make_spec("database", priority=90),
    )
    assert [record.spec.name for record in registry.ordered()] == ["database", "api", "gateway"]
    assert registry.dependency_report() == {"missing": [], "cycles": []}


def test_dependency_cycle_falls_back_without_failing_the_boot() -> None:
    registry = ModuleRegistry(DummyLogger())
    registry.register(
        make_dependency_module("a", ["b"]), make_spec("a", priority=10, depends_on=("b",))
    )
    registry.register(
        make_dependency_module("b", ["a"]), make_spec("b", priority=20, depends_on=("a",))
    )
    first = [record.spec.name for record in registry.ordered()]
    second = [record.spec.name for record in registry.ordered()]
    assert sorted(first) == ["a", "b"]  # a cycle cannot be ordered
    assert first == second  # but the fallback is deterministic
    assert registry.dependency_report()["cycles"] == ["b -> a"]


def test_unknown_dependency_is_reported_without_breaking_order() -> None:
    registry = ModuleRegistry(DummyLogger())
    registry.register(
        make_dependency_module("solo", ["ghost"]), make_spec("solo", depends_on=("ghost",))
    )
    assert [record.spec.name for record in registry.ordered()] == ["solo"]
    assert registry.dependency_report()["missing"] == ["solo -> ghost"]


def test_spec_validates_dependency_names() -> None:
    with pytest.raises(ModuleContractError, match="not a valid module name"):
        make_spec("worker", depends_on=("Bad Name",)).validate()
