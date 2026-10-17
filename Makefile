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
FLEET_ALLOW_MIXED ?= 0
FLEET_GPU_COUNT ?= 1
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
	workload-validate workload-preview fleet-compat-check fleet-plan fleet-first-worker-up fleet-control-up \
	fleet-first-worker-down fleet-check fleet-network-smoke fleet-gpu-install \
	fleet-certify-control fleet-gpu2-up fleet-gpu2-down fleet-cache-prepare fleet-large-image-pull \
	fleet-model-install fleet-observability-install fleet-cost-snapshot fleet-cost-install fleet-cost-check campaign-smoke campaign-text-replay campaign-run campaign-compare campaign-accounting campaign-report workload-generate \
	llmd-install llmd-smoke tenant-install fleet-down \
	lambda-contract-check lambda-plan lambda-check lambda-join lambda-cost-snapshot lambda-cost-install lambda-cost-check lambda-down \
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
	@echo "  workload-validate  Validate the reviewed multi-tenant pilot workload contract"
	@echo "  workload-preview  Preview deterministic metadata (TENANT, CLASS, TRIAL)"
	@echo "  fleet-plan      Read-only Vast offers, credit, and three-node budget preflight"
	@echo "  fleet-first-worker-up  Rent only the first owned GPU VM for control validation"
	@echo "  fleet-control-up  Rent only the staged, task-owned control-plane VM"
	@echo "  fleet-certify-control  Repeat two-node network/GPU gates before scaling"
	@echo "  fleet-gpu2-up  Rent the second worker only after the control checkpoint"
	@echo "  fleet-gpu2-down  Release an unjoined, exactly owned second worker (CONFIRM_DESTROY=1)"
	@echo "  fleet-first-worker-down  Destroy only the owned first GPU VM (CONFIRM_DESTROY=1)"
	@echo "  fleet-compat-check  Validate the pilot's immutable offline artifact contract"
	@echo "  fleet-check     Read-only exact node, GPU, and core-pod validation"
	@echo "  fleet-network-smoke  Temporary cross-node Pod/Service/DNS check (on control VM)"
	@echo "  fleet-gpu-install  Install GPU Operator and smoke each worker (on control VM)"
	@echo "  fleet-cache-prepare  Prepare a persistent model cache on an owned GPU worker"
	@echo "  fleet-large-image-pull  Bounded containerd timeout for large fleet images"
	@echo "  fleet-model-install  Deploy two KIP-owned vLLM replicas on the certified fleet"
	@echo "  fleet-observability-install  Install internal fleet Prometheus/Grafana/DCGM scrapes"
	@echo "  fleet-cost-snapshot  Record current task-owned Vast billed rates locally"
	@echo "  fleet-cost-install  Configure exact per-node OpenCost CSV prices on control VM"
	@echo "  fleet-cost-check  Reconcile a complete node-cost minute against Vast rates"
	@echo "  campaign-smoke  Run a reviewed authenticated AIPerf Gateway checkpoint"
	@echo "  campaign-text-replay  Replay a generated pilot text trace through AIPerf"
	@echo "  campaign-accounting  Reconcile a replay across AIPerf, Gateway, and EPP"
	@echo "  campaign-report  Publish a sanitized partial three-tenant text report"
	@echo "  workload-generate  Generate ignored, seeded AIPerf text traces from pilot-v1"
	@echo "  llmd-install   Install pinned private Envoy AI Gateway and llm-d request path"
	@echo "  llmd-smoke     Smoke the private Gateway (set GATEWAY_URL)"
	@echo "  tenant-install  Install fail-closed pilot tenant authorization on control VM"
	@echo "  fleet-down     Release only exact task-owned pilot VMs (CONFIRM_DESTROY=1)"
	@echo "  lambda-contract-check  Validate reviewed Lambda models/campaign/pins offline"
	@echo "  lambda-plan    Read-only Lambda capacity and rental prerequisites"
	@echo "  lambda-check   Inspect exact task-owned Lambda lease state"
	@echo "  lambda-join ROLE=small-1|large  Join an owned east-region GPU worker"
	@echo "  lambda-cost-snapshot  Cross-check task-owned Lambda node rates for OpenCost"
	@echo "  lambda-cost-install  Install pinned OpenCost with copied Lambda price files on control"
	@echo "  lambda-cost-check  Reconcile OpenCost node assets to Lambda listed rates on control"
	@echo "  campaign-run SCENARIO=nominal-text VARIANT=baseline|large-flow6|large-flow8 TRIAL=1  Run reviewed Lambda AIPerf text trial on control"
	@echo "  campaign-compare BASELINE_1=... BASELINE_2=... BASELINE_3=...  Compare three sanitized trials"
	@echo "  lambda-down CONFIRM_DESTROY=1 RUN_ID=<copied-report>  Release only task-owned Lambda VMs"
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
	$(UV) run python -m pytest

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
	@bash ./deploy/benchmark/setup.sh
	$(UV) python install 3.12.9
	$(UV) sync --python 3.12.9 --group benchmark --locked

benchmark-install: benchmark-setup
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

workload-validate:
	$(UV) run python -m bench workload-validate --contract pilot-v1

workload-preview:
	$(UV) run python -m bench workload-preview --contract pilot-v1 \
		--tenant "$(or $(TENANT),tenant-a)" --class "$(or $(CLASS),chat)" \
		--trial "$(or $(TRIAL),1)"

fleet-plan:
	@case "$(FLEET_ALLOW_MIXED)" in 0|1) ;; *) echo 'FLEET_ALLOW_MIXED must be 0 or 1' >&2; exit 2;; esac
	$(UV) run python -m fleet plan $(if $(filter 1,$(FLEET_ALLOW_MIXED)),--allow-mixed,)

fleet-first-worker-up:
	$(UV) run python -m fleet rent-first

fleet-control-up:
	$(UV) run python -m fleet rent-control

fleet-certify-control:
	$(UV) run python -m fleet certify-control

fleet-gpu2-up:
	$(UV) run python -m fleet rent-gpu2

fleet-gpu2-down:
	@test "$(CONFIRM_DESTROY)" = "1" || { echo 'Set CONFIRM_DESTROY=1' >&2; exit 2; }
	$(UV) run python -m fleet release-gpu2

fleet-first-worker-down:
	@test "$(CONFIRM_DESTROY)" = "1" || { echo 'Set CONFIRM_DESTROY=1' >&2; exit 2; }
	$(UV) run python -m fleet release-first

fleet-compat-check:
	$(UV) run python -m fleet compat-check

fleet-check:
	$(UV) run python -m fleet check --inventory "$(or $(INVENTORY),fleet/.work/pilot.json)"

fleet-network-smoke:
	./deploy/fleet/network-smoke.sh

fleet-gpu-install:
	@KIP_VERBOSE="$(VERBOSE)" KIP_FLEET_GPU_COUNT="$(FLEET_GPU_COUNT)" \
		./deploy/fleet/gpu-install.sh

fleet-cache-prepare:
	./deploy/fleet/cache-prepare.sh

fleet-large-image-pull:
	./deploy/fleet/large-image-pull.sh

fleet-model-install:
	./deploy/fleet/model-install.sh

fleet-observability-install:
	@KIP_VERBOSE="$(VERBOSE)" ./deploy/fleet/observability-install.sh

fleet-cost-snapshot:
	@$(UV) run python -m fleet.cost_ledger

fleet-cost-install:
	@KIP_VERBOSE="$(VERBOSE)" ./deploy/fleet/cost-install.sh

fleet-cost-check:
	@python3 -m fleet.cost_check

campaign-smoke:
	@UV_CACHE_DIR=.cache/uv $(UV) run --group benchmark --frozen python -m fleet.aiperf_smoke \
		--tenant "$(or $(TENANT),tenant-a)" --class "$(or $(WORKLOAD_CLASS),chat)"

campaign-text-replay:
	@UV_CACHE_DIR=.cache/uv $(UV) run --group benchmark --frozen python -m fleet.aiperf_text \
		--tenant "$(or $(TENANT),tenant-a)" --class "$(or $(WORKLOAD_CLASS),chat)" \
		--trial "$(or $(TRIAL),1)" --profile "$(or $(AIPERF_PROFILE),pilot-text-replay)" \
		$(if $(RUN_ID),--run-id "$(RUN_ID)",)

campaign-accounting:
	@UV_CACHE_DIR=.cache/uv $(UV) run --group benchmark --frozen python -m fleet.aiperf_accounting \
		--run-id "$(RUN_ID)" --tenant "$(or $(TENANT),tenant-a)" \
		--class "$(or $(WORKLOAD_CLASS),chat)"

campaign-report:
	@UV_CACHE_DIR=.cache/uv $(UV) run --group benchmark --frozen python -m bench.pilot_report \
		--run-id "$(RUN_ID)" $(if $(SCENARIO),--scenario "$(SCENARIO)",)

workload-generate:
	@UV_CACHE_DIR=.cache/uv $(UV) run --group benchmark --frozen python -m bench.pilot_dataset \
		--tenant "$(or $(TENANT),tenant-a)" --class "$(or $(WORKLOAD_CLASS),chat)" \
		--trial "$(or $(TRIAL),1)" --sessions "$(or $(SESSIONS),12)" --rate "$(or $(RATE),1.0)"

llmd-install:
	./deploy/fleet/llmd-install.sh

llmd-smoke:
	python3 ./deploy/fleet/llmd-smoke.py --url "$(GATEWAY_URL)" \
		--runs "$(or $(SMOKE_RUNS),1)" --parallel "$(or $(SMOKE_PARALLEL),1)"

tenant-install:
	./deploy/fleet/tenant-install.sh

fleet-down:
	@test "$(CONFIRM_DESTROY)" = "1" || { echo 'Set CONFIRM_DESTROY=1' >&2; exit 2; }
	$(UV) run python -m fleet release-fleet

lambda-contract-check:
	$(UV) run python -m fleet.lambda_cli contract

lambda-plan:
	$(UV) run python -m fleet.lambda_cli plan

lambda-check:
	$(UV) run python -m fleet.lambda_cli check

lambda-join:
	@case "$(ROLE)" in small-1|large) ;; *) echo 'ROLE must be small-1 or large' >&2; exit 2 ;; esac
	$(UV) run python -m fleet.lambda_join "$(ROLE)"

lambda-cost-snapshot:
	$(UV) run python -m fleet.lambda_cost_ledger

lambda-cost-install:
	@KIP_FLEET_COST_PROVIDER=lambda KIP_VERBOSE="$(VERBOSE)" ./deploy/fleet/cost-install.sh

lambda-cost-check:
	@python3 deploy/lambda/cost_check.py

campaign-run:
	@case "$(SCENARIO):$(VARIANT):$(TRIAL)" in \
		nominal-text:baseline:1|nominal-text:baseline:2|nominal-text:baseline:3|nominal-text:large-flow6:1|nominal-text:large-flow6:2|nominal-text:large-flow6:3|nominal-text:large-flow8:1|nominal-text:large-flow8:2|nominal-text:large-flow8:3) ;; \
		*) echo 'Only reviewed SCENARIO=nominal-text VARIANT=baseline|large-flow6|large-flow8 TRIAL=1|2|3 is implemented' >&2; exit 2 ;; \
	esac
	@$(UV) run --group benchmark --frozen python -m bench.lambda_nominal --trial "$(TRIAL)" --variant "$(VARIANT)"

campaign-compare:
	@$(UV) run python -m bench.lambda_compare --baseline \
		"$(BASELINE_1)" "$(BASELINE_2)" "$(BASELINE_3)" \
		$(if $(CANDIDATE_1),--candidate "$(CANDIDATE_1)" "$(CANDIDATE_2)" "$(CANDIDATE_3)",)

lambda-down:
	@test "$(CONFIRM_DESTROY)" = "1" || { echo 'Set CONFIRM_DESTROY=1' >&2; exit 2; }
	@CONFIRM_DESTROY=1 $(UV) run python -m fleet.lambda_down --report "$(RUN_ID)"

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
