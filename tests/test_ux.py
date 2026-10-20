"""UserExperience tests: menus, defaults, validation loops, non-interactive safety."""

from __future__ import annotations

import io

import pytest

from korodexios_core.cli.ux import MenuOption, UserExperience
from korodexios_core.config.defaults import CORE_DEFAULTS
from korodexios_core.config.layered import LayeredConfig
from korodexios_core.validate import require_port


def make_config(**overrides: object) -> LayeredConfig:
    merged = dict(CORE_DEFAULTS)
    merged.update(overrides)
    return LayeredConfig(defaults=merged)


def interactive_ux(answers: list[str], **overrides: object) -> tuple[UserExperience, io.StringIO]:
    buffer = io.StringIO()
    iterator = iter(answers)
    config = make_config(**{"core.cli.interactive": True, "core.cli.color": False, **overrides})
    ux = UserExperience(config, input_fn=lambda prompt: next(iterator), stdout=buffer)
    return ux, buffer


def test_non_interactive_never_reads_input() -> None:
    buffer = io.StringIO()

    def boom(prompt: str) -> str:
        raise AssertionError("non-interactive mode must not read stdin")

    config = make_config(**{"core.cli.interactive": False, "core.cli.color": False})
    ux = UserExperience(config, input_fn=boom, stdout=buffer)
    options = [MenuOption("Run"), MenuOption("Configure")]
    assert ux.menu("Action", options, default_index=2) == 2
    assert ux.ask_text("Name", default="korodex") == "korodex"
    assert ux.ask_int("Port", default=8080, minimum=1, maximum=65535) == 8080
    assert ux.ask_float("Ratio", default=1.5) == 1.5
    assert ux.confirm("Dry run", default=False) is False
    assert ux.ask_choice("Mode", ["fast", "safe"], default="safe") == "safe"
    assert ux.ask_path("Source", default="src").name == "src"
    assert "[i]" in buffer.getvalue()


def test_menu_accepts_number_label_or_enter() -> None:
    ux, _ = interactive_ux(["2"])
    options = [MenuOption("Run"), MenuOption("Configure")]
    assert ux.menu("Action", options) == 2

    ux, _ = interactive_ux(["Configure"])
    assert ux.menu("Action", options) == 2

    ux, _ = interactive_ux([""])
    assert ux.menu("Action", options, default_index=2) == 2


def test_menu_reprompts_on_invalid_choice() -> None:
    ux, buffer = interactive_ux(["banana", "99", "1"])
    options = [MenuOption("Run"), MenuOption("Configure")]
    assert ux.menu("Action", options) == 1
    printed = buffer.getvalue()
    assert "[!] expected a number" in printed
    assert "[!] pick a number between 1 and 2" in printed


def test_menu_skips_disabled_option() -> None:
    ux, buffer = interactive_ux(["2"])
    options = [MenuOption("Run"), MenuOption("Retire", disabled=True)]
    assert ux.menu("Action", options) == 1
    assert "[!] option [2] is not available" in buffer.getvalue()


def test_ask_text_uses_default_on_enter_and_validates_otherwise() -> None:
    ux, _ = interactive_ux([""])
    assert ux.ask_text("Port", default="8080", validator=require_port) == "8080"

    ux, buffer = interactive_ux(["99999", "8080"])
    assert ux.ask_text("Port", default="8080", validator=require_port) == "8080"
    assert "[!]" in buffer.getvalue() and "65535" in buffer.getvalue()


def test_ask_text_required_reprompts_on_empty() -> None:
    ux, buffer = interactive_ux(["", "   ", "korodex"])
    assert ux.ask_text("Name", required=True) == "korodex"
    assert buffer.getvalue().count("[!]") >= 1


def test_confirm_loops_until_a_real_answer() -> None:
    ux, buffer = interactive_ux(["maybe", "y"])
    assert ux.confirm("Dry run", default=True) is True
    assert "answer yes or no" in buffer.getvalue()

    ux, _ = interactive_ux(["n"])
    assert ux.confirm("Dry run", default=True) is False


def test_closed_input_falls_back_to_defaults() -> None:
    config = make_config(**{"core.cli.interactive": True, "core.cli.color": False})
    buffer = io.StringIO()

    def closed(prompt: str) -> str:
        raise EOFError("stdin closed")

    ux = UserExperience(config, input_fn=closed, stdout=buffer)
    assert ux.menu("Action", [MenuOption("Run"), MenuOption("Configure")], default_index=2) == 2
    assert ux.ask_text("Name", default="korodex", required=True) == "korodex"
    assert ux.confirm("Dry run") is True
    assert not ux.interactive
    assert "input closed" in buffer.getvalue()


def test_status_tags_are_written_to_stdout() -> None:
    ux, buffer = interactive_ux([])
    ux.status("done")
    ux.note("info")
    ux.error("boom")
    ux.key_values([("key", "value"), ("flag", True), ("none", None)])
    printed = buffer.getvalue()
    for marker in ("[✓] done", "[i] info", "[!] boom", "flag", "yes", "none", "-"):
        assert marker in printed


def test_menu_requires_options() -> None:
    ux, _ = interactive_ux([])
    with pytest.raises(ValueError, match="at least one option"):
        ux.menu("Empty", [])


def test_status_falls_back_to_ascii_tags_on_legacy_streams() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    config = make_config(**{"core.cli.interactive": False, "core.cli.color": False})
    ux = UserExperience(config, stdout=stream)
    ux.status("chassis stopped")
    stream.flush()
    assert stream.buffer.getvalue().decode("cp1252").strip() == "[OK] chassis stopped"
