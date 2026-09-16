.DEFAULT_GOAL := help

UV ?= uv
VERBOSE ?= 0
GPU_REQUIRED ?= 0
GPU_DRIVER_MODE ?=
RUNTIME ?= vllm
MODEL_PROFILE ?= qwen2.5-7b-instruct
CONFIRM_DESTROY ?=
NODE_IP ?=
POD_CIDR ?=
SERVICE_CIDR ?=
DEPLOY_SCRIPTS := $(shell find deploy -type f -name '*.sh' -print | sort)

.PHONY: help setup lint format-check deploy-script-check host-script-check test check \
	bench-help host-check host-setup cluster-check cluster-bootstrap gpu-check gpu-install \
	gpu-smoke runtime-check runtime-deploy runtime-smoke destroy

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
	@echo "  gpu-check      Validate the local NVIDIA GPU stack without mutation"
	@echo "  gpu-install    Install and validate the NVIDIA GPU Operator"
	@echo "  gpu-smoke      Run a temporary in-cluster nvidia-smi smoke test"
	@echo "  runtime-check  Validate the selected inference runtime without generation"
	@echo "  runtime-deploy  Deploy and validate the selected inference runtime"
	@echo "  runtime-smoke  Run temporary OpenAI-compatible inference checks"
	@echo "  destroy        Remove the KIP-managed runtime, GPU, and Kubernetes stack"

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

gpu-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_DRIVER_MODE="$(GPU_DRIVER_MODE)" \
		./deploy/gpu/validate.sh

gpu-install:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_DRIVER_MODE="$(GPU_DRIVER_MODE)" \
		./deploy/gpu/install.sh

gpu-smoke:
	@KIP_VERBOSE="$(VERBOSE)" KIP_GPU_DRIVER_MODE="$(GPU_DRIVER_MODE)" \
		./deploy/gpu/smoke.sh

runtime-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_RUNTIME="$(RUNTIME)" \
		KIP_MODEL_PROFILE="$(MODEL_PROFILE)" ./deploy/runtime/dispatch.sh check

runtime-deploy:
	@KIP_VERBOSE="$(VERBOSE)" KIP_RUNTIME="$(RUNTIME)" \
		KIP_MODEL_PROFILE="$(MODEL_PROFILE)" ./deploy/runtime/dispatch.sh deploy

runtime-smoke:
	@KIP_VERBOSE="$(VERBOSE)" KIP_RUNTIME="$(RUNTIME)" \
		KIP_MODEL_PROFILE="$(MODEL_PROFILE)" ./deploy/runtime/dispatch.sh smoke

destroy:
	@KIP_VERBOSE="$(VERBOSE)" KIP_CONFIRM_DESTROY="$(CONFIRM_DESTROY)" \
		./deploy/destroy.sh
