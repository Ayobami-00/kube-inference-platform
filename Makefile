.DEFAULT_GOAL := help

UV ?= uv
VERBOSE ?= 0
GPU_REQUIRED ?= 0
HOST_SCRIPTS := $(shell find deploy/host -type f -name '*.sh' -print | sort)

.PHONY: help setup lint format-check host-script-check test check bench-help host-check host-setup

help:
	@echo "Available targets:"
	@echo "  setup         Create the development environment"
	@echo "  lint          Run Ruff lint checks"
	@echo "  format-check  Verify Ruff formatting"
	@echo "  host-script-check  Verify host script syntax"
	@echo "  test          Run the test suite"
	@echo "  check         Run all quality checks"
	@echo "  bench-help    Show the benchmark CLI help"
	@echo "  host-check    Validate the local target host"
	@echo "  host-setup    Prepare and validate the local target host"

setup:
	$(UV) sync

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

host-script-check:
	bash -n $(HOST_SCRIPTS)

test:
	$(UV) run pytest

check: lint format-check host-script-check test

bench-help:
	$(UV) run python -m bench --help

host-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_REQUIRED="$(GPU_REQUIRED)" ./deploy/host/validate.sh

host-setup:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_REQUIRED="$(GPU_REQUIRED)" ./deploy/host/setup.sh
