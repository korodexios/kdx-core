# korodexios-core developer shortcuts (uv-first, pip-compatible fallbacks).
UV ?= uv
PY ?= $(UV) run python

.PHONY: help install sync test coverage lint typecheck fmt check clean run doctor modules config demo

help:
	@echo "korodexios-core targets:"
	@echo "  make install     install the project with dev extras into .venv"
	@echo "  make test        run the pytest suite"
	@echo "  make coverage    run the suite and enforce the coverage floor"
	@echo "  make lint        run ruff checks"
	@echo "  make typecheck   run mypy"
	@echo "  make check       lint + typecheck + coverage"
	@echo "  make run         boot the chassis (Ctrl+C for a clean abort)"
	@echo "  make doctor      print the environment inspection report"
	@echo "  make modules     list discovered modules"
	@echo "  make config      show resolved configuration with provenance"

install:
	$(UV) venv
	$(UV) pip install -e ".[dev]"

sync:
	$(UV) sync --extra dev

test:
	$(PY) -m pytest -q

# The floor is a ratchet: it is set one point under the measured total, so a
# removed test or a new untested branch fails the bar instead of sliding by.
COVERAGE_FLOOR ?= 82

coverage:
	$(PY) -m pytest -q --cov=korodexios_core --cov-report=term-missing --cov-fail-under=$(COVERAGE_FLOOR)

lint:
	$(UV) run ruff check src tests

typecheck:
	$(UV) run mypy

fmt:
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

check: lint typecheck coverage

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build var

run:
	$(UV) run korodex run

doctor:
	$(UV) run korodex doctor

modules:
	$(UV) run korodex modules list

config:
	$(UV) run korodex config show

demo:
	$(UV) run korodex run --once
