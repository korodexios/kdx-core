# korodexios-core developer shortcuts (uv-first, pip-compatible fallbacks).
# The runner resolves itself: with uv on PATH it is 'uv run ', without it the
# project venv's bin directory ('.venv/bin/'). Override RUN only if you keep
# tools elsewhere; keep the join style (a trailing space for 'uv run ', none for
# a bin directory).
UV := $(shell command -v uv 2>/dev/null)
RUN ?= $(if $(UV),$(UV) run ,.venv/bin/)
PY ?= $(if $(PYTHON),$(PYTHON),$(RUN)python)

.PHONY: help install sync test coverage lint typecheck fmt check clean run doctor modules config demo

help:
	@echo "korodexios-core targets:"
	@echo "  make install     install the project with dev extras into .venv (needs uv)"
	@echo "  make test        run the pytest suite"
	@echo "  make coverage    run the suite and enforce the coverage floor"
	@echo "  make lint        run ruff checks"
	@echo "  make typecheck   run mypy"
	@echo "  make check       lint + typecheck + coverage"
	@echo "  make run         boot the chassis (Ctrl+C for a clean abort)"
	@echo "  make doctor      print the environment inspection report"
	@echo "  make modules     list discovered modules"
	@echo "  make config      show resolved configuration with provenance"
	@echo "without uv on PATH: create .venv first (python3 -m venv .venv && pip install -e '.[dev]')"

install:
	$(UV) venv
	$(UV) pip install -e ".[dev]"

sync:
	$(UV) sync --extra dev

test:
	$(PY) -m pytest -q

# The floor is a ratchet: it is set one point under the measured total, so a
# removed test or a new untested branch fails the bar instead of sliding by.
# Measured 88.4% (542 tests) on the Core API v2 surface at the v0.3.1 tag.
COVERAGE_FLOOR ?= 88

coverage:
	$(PY) -m pytest -q --cov=korodexios_core --cov-report=term-missing --cov-fail-under=$(COVERAGE_FLOOR)

lint:
	$(RUN)ruff check src tests

typecheck:
	$(RUN)mypy

fmt:
	$(RUN)ruff check --fix src tests
	$(RUN)ruff format src tests

check: lint typecheck coverage

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build var

run:
	$(RUN)kdx run

doctor:
	$(RUN)kdx doctor

modules:
	$(RUN)kdx modules list

config:
	$(RUN)kdx config show

demo:
	$(RUN)kdx run --once
