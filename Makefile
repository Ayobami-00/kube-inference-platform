.DEFAULT_GOAL := help

UV ?= uv
VERBOSE ?= 0
GPU_REQUIRED ?= 0
GPU_DRIVER_MODE ?=
RUNTIME ?= vllm
MODEL_PROFILE ?= qwen2.5-7b-instruct
COST_PROFILE ?=
SUITE ?= baseline
RUN_ID ?=
RETAIN_DAYS ?= 30
SERVE_HOST ?= 127.0.0.1
SERVE_PORT ?= 8008
CONFIRM_DESTROY ?=
ARTIFACT_VALIDATOR_CACHE ?= $(CURDIR)/.cache/artifact-validators
PROMTOOL ?= $(ARTIFACT_VALIDATOR_CACHE)/bin/promtool
HELM ?= $(ARTIFACT_VALIDATOR_CACHE)/bin/helm
KUBE_PROMETHEUS_STACK_CHART_ARCHIVE ?= $(ARTIFACT_VALIDATOR_CACHE)/charts/kube-prometheus-stack.tgz
OPENCOST_CHART_ARCHIVE ?= $(ARTIFACT_VALIDATOR_CACHE)/charts/opencost.tgz
NODE_IP ?=
POD_CIDR ?=
SERVICE_CIDR ?=
DEPLOY_SCRIPTS := $(shell find deploy -type f -name '*.sh' -print | sort)

.PHONY: help setup artifact-validator-setup lint format-check deploy-script-check \
	host-script-check artifact-check artifact-structural-check \
	artifact-acceptance-check test check \
	bench-help host-check host-setup cluster-check cluster-bootstrap gpu-check gpu-install \
	gpu-smoke runtime-check runtime-deploy runtime-smoke observability-check \
	observability-install observability-smoke cost-configure cost-check cost-show \
	benchmark-setup benchmark-install benchmark-check benchmark-help benchmark \
	benchmark-resume benchmark-report benchmark-list benchmark-serve benchmark-clean \
	bootstrap destroy

help:
	@echo "Available targets:"
	@echo "  setup         Create the development environment and validator cache"
	@echo "  artifact-validator-setup  Cache checksum-verified validation tools/charts"
	@echo "  lint          Run Ruff lint checks"
	@echo "  format-check  Verify Ruff formatting"
	@echo "  deploy-script-check  Verify deployment script syntax"
	@echo "  host-script-check  Compatibility alias for deploy-script-check"
	@echo "  artifact-check  Strictly validate dashboards, rules, exact charts, and publishable files"
	@echo "  artifact-structural-check  Run only the explicit offline structural subset"
	@echo "  artifact-acceptance-check  Compatibility alias for artifact-check"
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
	@echo "  observability-install  Install Prometheus, Grafana, OpenCost, and metric adapters"
	@echo "  observability-check  Validate observability without mutation"
	@echo "  observability-smoke  Verify required metric and cost queries"
	@echo "  cost-configure  Apply a committed whole-node USD cost profile"
	@echo "  cost-check     Validate active OpenCost pricing"
	@echo "  cost-show      Show the active public pricing metadata"
	@echo "  benchmark-setup  Install the pinned AIPerf development dependency group"
	@echo "  benchmark-install  Install the in-cluster AIPerf Job foundation"
	@echo "  benchmark-check  Validate benchmark prerequisites"
	@echo "  benchmark      Run a committed benchmark suite"
	@echo "  benchmark-resume  Resume an interrupted, ownership-validated benchmark run"
	@echo "  benchmark-report  Rebuild a sanitized benchmark report"
	@echo "  benchmark-list  List committed suites and local runs"
	@echo "  benchmark-serve  Serve a generated report on localhost"
	@echo "  benchmark-help   Show the benchmark platform commands"
	@echo "  benchmark-clean  Remove expired local work and node result archives"
	@echo "  bootstrap      Compose the complete v0.1 platform installation"
	@echo "  destroy        Remove KIP-managed platform state for a fresh run"

setup:
	$(UV) sync
	@$(MAKE) --no-print-directory artifact-validator-setup

artifact-validator-setup:
	@KIP_ARTIFACT_VALIDATOR_CACHE="$(ARTIFACT_VALIDATOR_CACHE)" \
		./scripts/setup_artifact_validators.sh

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

deploy-script-check:
	bash -n $(DEPLOY_SCRIPTS)

host-script-check: deploy-script-check

artifact-check:
	@PROMTOOL="$(PROMTOOL)" HELM="$(HELM)" \
		KIP_KUBE_PROMETHEUS_STACK_CHART_ARCHIVE="$(KUBE_PROMETHEUS_STACK_CHART_ARCHIVE)" \
		KIP_OPENCOST_CHART_ARCHIVE="$(OPENCOST_CHART_ARCHIVE)" \
		$(UV) run python scripts/validate_artifacts.py --strict-external

artifact-structural-check:
	@$(UV) run python scripts/validate_artifacts.py --structural-only

artifact-acceptance-check: artifact-check

test:
	$(UV) run pytest

check: lint format-check deploy-script-check artifact-check test

bench-help:
	$(UV) run --group benchmark python -m bench --help

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

observability-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/validate.sh

observability-install:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/install.sh

observability-smoke:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/smoke.sh

cost-configure:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/cost.sh configure

cost-check:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/cost.sh check

cost-show:
	@KIP_VERBOSE="$(VERBOSE)" KIP_COST_PROFILE="$(COST_PROFILE)" \
		./deploy/observability/cost.sh show

benchmark-setup:
	$(UV) sync --group benchmark

benchmark-install:
	$(UV) run --group benchmark python -m bench install

benchmark-check:
	$(UV) run --group benchmark python -m bench check --cluster \
		$(if $(strip $(COST_PROFILE)),--cost-profile "$(COST_PROFILE)",)

benchmark-help: bench-help

benchmark:
	$(UV) run --group benchmark python -m bench run --suite "$(SUITE)" \
		--cost-profile "$(COST_PROFILE)" $(if $(strip $(RUN_ID)),--run-id "$(RUN_ID)",)

benchmark-resume:
	$(UV) run --group benchmark python -m bench run --suite "$(SUITE)" \
		--cost-profile "$(COST_PROFILE)" --run-id "$(RUN_ID)" --resume

benchmark-report:
	$(UV) run --group benchmark python -m bench report --run-id "$(RUN_ID)"

benchmark-list:
	$(UV) run --group benchmark python -m bench list

benchmark-serve:
	$(UV) run --group benchmark python -m bench serve --run-id "$(RUN_ID)" \
		--host "$(SERVE_HOST)" --port "$(SERVE_PORT)"

benchmark-clean:
	$(UV) run --group benchmark python -m bench clean --retain-days "$(RETAIN_DAYS)"

bootstrap:
	@$(MAKE) --no-print-directory host-setup VERBOSE="$(VERBOSE)"
	@$(MAKE) --no-print-directory cluster-bootstrap VERBOSE="$(VERBOSE)" \
		NODE_IP="$(NODE_IP)" POD_CIDR="$(POD_CIDR)" SERVICE_CIDR="$(SERVICE_CIDR)"
	@$(MAKE) --no-print-directory gpu-install VERBOSE="$(VERBOSE)" \
		GPU_DRIVER_MODE="$(GPU_DRIVER_MODE)"
	@$(MAKE) --no-print-directory runtime-deploy VERBOSE="$(VERBOSE)" \
		RUNTIME="$(RUNTIME)" MODEL_PROFILE="$(MODEL_PROFILE)"
	@$(MAKE) --no-print-directory observability-install VERBOSE="$(VERBOSE)" \
		COST_PROFILE="$(COST_PROFILE)"
	@$(MAKE) --no-print-directory benchmark-install

destroy:
	@KIP_VERBOSE="$(VERBOSE)" KIP_CONFIRM_DESTROY="$(CONFIRM_DESTROY)" \
		./deploy/destroy.sh
