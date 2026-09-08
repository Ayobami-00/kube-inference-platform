.DEFAULT_GOAL := help

UV ?= uv
VERBOSE ?= 0
GPU_REQUIRED ?= 0
NODE_IP ?=
POD_CIDR ?=
SERVICE_CIDR ?=
DEPLOY_SCRIPTS := $(shell find deploy -type f -name '*.sh' -print | sort)

.PHONY: help setup lint format-check deploy-script-check host-script-check test check \
	bench-help host-check host-setup cluster-check cluster-bootstrap

help:
	@echo "Available targets:"
	@echo "  setup         Create the development environment"
	@echo "  lint          Run Ruff lint checks"
	@echo "  format-check  Verify Ruff formatting"
	@echo "  deploy-script-check  Verify deployment script syntax"
	@echo "  host-script-check  Compatibility alias for deploy-script-check"
	@echo "  test          Run the test suite"
	@echo "  check         Run all quality checks"
	@echo "  bench-help    Show the benchmark CLI help"
	@echo "  host-check    Validate the local target host"
	@echo "  host-setup    Prepare and validate the local target host"
	@echo "  cluster-check  Validate the local single-node Kubernetes cluster"
	@echo "  cluster-bootstrap  Bootstrap the local single-node Kubernetes cluster"

setup:
	$(UV) sync

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

deploy-script-check:
	bash -n $(DEPLOY_SCRIPTS)

host-script-check: deploy-script-check

test:
	$(UV) run pytest

check: lint format-check deploy-script-check test

bench-help:
	$(UV) run python -m bench --help

host-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_REQUIRED="$(GPU_REQUIRED)" ./deploy/host/validate.sh

host-setup:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_REQUIRED="$(GPU_REQUIRED)" ./deploy/host/setup.sh

cluster-check:
	@KIP_VERBOSE="$(VERBOSE)" ./deploy/cluster/validate.sh

cluster-bootstrap:
	@KIP_VERBOSE="$(VERBOSE)" KIP_NODE_IP="$(NODE_IP)" KIP_POD_CIDR="$(POD_CIDR)" \
		KIP_SERVICE_CIDR="$(SERVICE_CIDR)" ./deploy/cluster/bootstrap.sh
