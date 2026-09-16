# Kube Inference Platform

Kube Inference Platform is a portable, Kubernetes-native LLM inference platform for
turning bare-metal NVIDIA GPU servers into single- or multi-node inference clusters. The
long-term platform will provide declarative model deployment, topology-aware placement,
distributed execution, routing, autoscaling, SLO management, observability, and cost
optimisation.

> [!IMPORTANT]
> The repository currently contains the v0.1 foundation, portable host preparation,
> single-node Kubernetes bootstrap, and NVIDIA GPU stack tooling. These layers are tested
> with deterministic fixtures and have passed the complete host-to-CUDA path on target
> NVIDIA A10 hardware. vLLM serving, observability, and real benchmark traffic are not yet
> implemented or validated.

## v0.1 objective

The v0.1 milestone establishes one complete, production-shaped vertical slice:

```text
bare-metal NVIDIA GPU
        ↓
single-node Kubernetes
        ↓
NVIDIA GPU Operator
        ↓
vLLM model server
        ↓
OpenAI-compatible streaming API
        ↓
concurrent benchmark client
        ↓
latency, throughput, and GPU telemetry
```

The goal is not to build the full control plane. The goal is to prove that a real model can
serve real requests on a real GPU and that the complete request path can be measured.

See [the architecture document](docs/architecture.md) for component boundaries and data
flow.

## Repository layout

```text
.
├── bench/                  Python benchmark CLI
├── deploy/                 Host, cluster, GPU, runtime, and observability assets
├── docs/                   Architecture documentation
├── experiments/baseline/   Reproducible baseline experiment record
├── tests/                  Benchmark and deployment-tooling tests
├── Makefile                Local development commands
└── pyproject.toml          Python project and tool configuration
```

Operational instructions remain in this README. A separate runbook will only be introduced
if multi-node operations, upgrades, or recovery procedures make this document unwieldy.

## Local development

### Prerequisites

- Python 3.12 for the project development environment (the code supports Python 3.11 or newer)
- [uv](https://docs.astral.sh/uv/)
- Make

Create the development environment:

```bash
make setup
```

Run all local checks:

```bash
make check
```

Inspect the initial benchmark command:

```bash
python -m bench --help
```

The CLI is intentionally limited to a runnable help command at this stage. HTTP/SSE traffic,
concurrency, token accounting, and metrics are added during the benchmark implementation
step.

## Host preparation

The v0.1 host tooling runs directly on a target machine rather than orchestrating it remotely.
From a repository checkout on the host, run:

```bash
make host-setup
```

On a minimal image where `make` is not installed yet, invoke the underlying setup once; it
installs `make` along with the inspection prerequisites, after which the public Make targets
are available:

```bash
./deploy/host/setup.sh
```

The setup command installs only missing inspection prerequisites and then validates the host.
It supports root execution or a non-root user with `sudo`, and it is safe to repeat. It does
not install or configure containerd, Kubernetes, NVIDIA software, swap, kernel modules,
sysctls, or networking.

The v0.1 target contract is:

- Ubuntu 22.04 or 24.04
- x86-64
- systemd
- `apt` package management
- Root access or working `sudo`
- A usable non-loopback network interface

Run validation without changing the host:

```bash
make host-check
```

Display the complete output from hardware inspection commands:

```bash
make host-check VERBOSE=1
```

Before GPU Operator installation, unavailable NVIDIA hardware and driver checks are reported
as `DEFERRED`. `GPU_REQUIRED=1` is the strict host-level check for the explicit
preinstalled-driver workflow:

```bash
make host-check GPU_REQUIRED=1
```

Operator-managed drivers live in the Operator's host-mounted driver root, so Kubernetes-level
`gpu-check` and `gpu-smoke` are authoritative for the default Operator-managed workflow.

Both `VERBOSE` and `GPU_REQUIRED` accept only `0` or `1`. Terminal colors are enabled only for
interactive output and can be disabled explicitly with `NO_COLOR=1`. Host details are printed
to the terminal and are never persisted automatically.

The later multi-node milestone will add inventory-driven remote orchestration while reusing
these same target-local scripts.

## Single-node Kubernetes bootstrap

After host preparation, bootstrap Kubernetes directly on the target:

```bash
make cluster-bootstrap
```

The v0.1 cluster is pinned to Kubernetes 1.36.4, containerd 2.3.4, Cilium 1.20.1,
Cilium CLI 0.19.7, and Helm 3.21.4. The node runs both the control-plane and worker roles;
kube-proxy remains enabled, and Cilium uses Kubernetes PodCIDR IPAM with an IPv4 VXLAN
overlay. Hubble remains deferred to the observability milestone.

The default network ranges are:

```text
Pod CIDR:      10.244.0.0/16
Service CIDR:  10.96.0.0/12
DNS domain:    cluster.local
```

The bootstrap derives the node IPv4 address from a single default route. Specify it when the
host has multiple default routes, and override network ranges when they conflict with target
host or provider routes:

```bash
make cluster-bootstrap NODE_IP=192.0.2.10
make cluster-bootstrap POD_CIDR=172.20.0.0/16 SERVICE_CIDR=172.21.0.0/16
```

The command makes the following persistent host changes:

- Installs only the pinned container runtime and Kubernetes tooling, plus verified Helm and
  Cilium CLI binaries. It never performs a package upgrade or installs Docker Engine.
- Loads `overlay` and `br_netfilter`, enables bridge packet filtering and IPv4 forwarding,
  and writes project-owned files under `/etc/modules-load.d` and `/etc/sysctl.d`.
- Disables swap and creates `/etc/fstab.kip-pre-bootstrap` before marking swap entries in
  `/etc/fstab`. Swap managed outside that file is rejected instead of changed blindly.
- Writes the containerd and `crictl` configuration, initializes kubeadm, installs Cilium,
  and copies the administrative kubeconfig to the invoking user's `~/.kube/config` with
  restrictive permissions.

Between `kubeadm init` and Cilium installation, a `NotReady` node and pending CoreDNS pods are
expected transitional states. Completion is reported only after containerd, kubelet, the API
readiness endpoint, all control-plane components, kube-proxy, Cilium, CoreDNS, DNS resolution,
Service routing, and Cilium connectivity pass.

Re-running the command reconciles a healthy matching cluster and reruns smoke tests. It stops
on partial kubeadm state, version drift, changed immutable CIDRs, or an unrelated existing
runtime. It never runs `kubeadm reset` or deletes etcd data automatically.

Run read-only validation at any time:

```bash
make cluster-check
make cluster-check VERBOSE=1
```

The validator reads current services and cluster resources but creates no pods and writes no
report. The mutating DNS, Service, and Cilium connectivity smoke tests run only during
`cluster-bootstrap`.

Normal administration remains inside the SSH session on the target. The bootstrap does not
disable the host firewall or open NodePort ranges. If remote API access is enabled, restrict
TCP 6443 to trusted administrator addresses in both the provider and host firewalls. Treat
`~/.kube/config` and `/etc/kubernetes/admin.conf` as administrative credentials and never
commit or print them.

## NVIDIA GPU stack

After `make cluster-bootstrap` passes, install the GPU stack directly on the target:

```bash
make gpu-install
```

The default `GPU_DRIVER_MODE=operator` lets GPU Operator install the pinned NVIDIA driver. A
machine with a provider-installed driver must opt into the alternate mode, and its host
`nvidia-smi` and topology checks must pass before the cluster is changed:

```bash
make host-check GPU_REQUIRED=1
make gpu-install GPU_DRIVER_MODE=preinstalled
```

Operator-managed mode also requires the exact running-kernel header package to remain
downloadable from the configured Ubuntu repositories. This is stricter than merely having
headers installed on the host because the driver container resolves its own build packages.
If an image boots an obsolete kernel whose headers have left the repositories, update the
kernel and reboot the target before running `make gpu-install`; the tooling never reboots it.

The v0.1 stack pins GPU Operator v26.7.0 and driver 595.91.07. Before Helm runs, the installer
downloads Cosign into a temporary directory, checks its pinned checksum, confirms that the
chart tag resolves to the committed OCI digest, and verifies NVIDIA's signature. Helm then
installs that immutable chart digest as release `gpu-operator` in namespace `gpu-operator`.
The selected driver mode and digest are stored in the release values.

CDI is enabled, NRI is disabled, and ordinary Pods request the standard `nvidia.com/gpu`
resource without a `runtimeClassName`. The NVIDIA toolkit writes its containerd drop-in under
`/etc/containerd/conf.d`, which is already imported by the project-owned containerd
configuration. Node Feature Discovery, the toolkit, Device Plugin, GPU Feature Discovery,
and validators are enabled. DCGM Exporter is deferred to observability; MIG, time-slicing,
DRA, and all other shared or partitioned allocation modes are disabled.

The installer is deliberately a hard checkpoint. It requires Operator and operand health,
requires Kubernetes capacity and allocatable `nvidia.com/gpu` to match the physical PCI GPU
count, and runs a temporary Pod that requests one GPU and executes `nvidia-smi`. The Pod is
deleted on success or failure, and cluster health is checked again. Do not proceed to vLLM
unless the command completes successfully.

Run read-only validation or repeat only the temporary workload independently:

```bash
make gpu-check
make gpu-check VERBOSE=1
make gpu-smoke
make gpu-smoke VERBOSE=1
```

Safe reruns reconcile only the same release, namespace, chart version and digest, driver mode,
and v0.1 values. Version, mode, allocation, or unmanaged-installation drift stops with
diagnostics; it is never adopted or upgraded automatically. A failed Helm installation is
not rolled back automatically so its Pods remain available for inspection. The tooling never
uninstalls a host driver, unloads kernel modules, deletes NVIDIA CRDs, edits GRUB, blacklists
modules, resets GPUs, repairs Secure Boot, or reboots the machine.

GPU details remain terminal-only. Treat kubeconfig access and all registry credentials as
sensitive, keep TCP 6443 restricted, and use `VERBOSE=1` only where terminal output is handled
appropriately.

## Platform teardown

To return a target to the pre-cluster state for a fresh bootstrap, run:

```bash
make destroy
```

The interactive command requires typing `destroy`. For an explicitly authorized
non-interactive run, use:

```bash
make destroy CONFIRM_DESTROY=1
```

Teardown first verifies the KIP ownership markers and pinned package versions. It then
uninstalls the matching GPU Operator, keeps Cilium alive while `kubeadm reset` stops remaining
Pod sandboxes, and removes Cilium namespaces, interfaces, CNI binaries, pinned BPF objects,
and only firewall rules in `CILIUM_` or `KUBE-` chains. It then unmounts project runtime
mounts, removes KIP cluster/runtime data and the pinned Kubernetes/containerd packages,
restores the original `fstab`, and removes the project-owned sysctl and modules-load files. A
matching user kubeconfig is removed; a changed kubeconfig is preserved. A project-owned
teardown marker makes interrupted runs safely resumable.

The command deliberately preserves SSH access, the operating system, kernel packages,
inspection prerequisites installed by `host-setup`, and shared Helm/Cilium CLI binaries. It
does not unload GPU kernel modules or reboot the target. Unrecognized ownership, an unmanaged
GPU Operator release, or package-version drift causes teardown to stop instead of deleting
state blindly. After a successful teardown, start again with:

```bash
make host-setup
make cluster-bootstrap
make gpu-install
```

## v0.1 implementation milestones

- [x] Create the repository foundation and runnable benchmark skeleton.
- [x] Implement portable host preparation and validation tooling.
- [x] Implement single-node Kubernetes bootstrap and CNI installation tooling.
- [x] Bootstrap and validate the single-node cluster on target hardware.
- [x] Implement NVIDIA GPU Operator installation and CUDA validation tooling.
- [x] Validate the NVIDIA GPU stack and CUDA smoke Pod on target hardware.
- [x] Provision the target host and validate the host, cluster, and GPU slices.
- [ ] Deploy one vLLM model replica and expose it through a Kubernetes Service.
- [ ] Verify non-streaming and streaming OpenAI-compatible requests.
- [ ] Implement the asynchronous benchmark client and inference metrics.
- [ ] Install Prometheus, Grafana, and DCGM Exporter.
- [ ] Run and document the baseline concurrency experiment.

Deployment assets are added in that order. Each command documented here must be verified on
the real machine before its checklist item is marked complete.

## Security and local state

Never commit SSH keys, API tokens, kubeconfig files, real machine inventories, model-provider
credentials, or Kubernetes Secret values. Use local ignored files and environment variables,
and commit only clearly marked examples with placeholder values.

Raw benchmark traces and model caches remain local. Compact experiment results such as CSV
and JSON summaries should be committed with the configuration that produced them.

## v0.1 Definition of Done

The v0.1 milestone is complete when a real model runs on a real NVIDIA GPU inside bare-metal
Kubernetes, serves streaming OpenAI-compatible requests, and can be concurrently benchmarked
while application and GPU telemetry are collected.

The Go operator, custom gateway, authentication, rate limiting, autoscaling, multi-replica
routing, and multi-node inference are deliberately deferred until this baseline is proven.
