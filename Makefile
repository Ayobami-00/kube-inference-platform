.DEFAULT_GOAL := help

UV ?= uv

.PHONY: help setup lint format-check test check bench-help

help:
	@echo "Available targets:"
	@echo "  setup         Create the development environment"
	@echo "  lint          Run Ruff lint checks"
	@echo "  format-check  Verify Ruff formatting"
	@echo "  test          Run the test suite"
	@echo "  check         Run all quality checks"
	@echo "  bench-help    Show the benchmark CLI help"

setup:
	$(UV) sync

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

test:
	$(UV) run pytest

check: lint format-check test

bench-help:
	$(UV) run python -m bench --help
