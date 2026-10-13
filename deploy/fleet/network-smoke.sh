#!/usr/bin/env bash
# Temporary cross-node Pod-IP, ClusterIP, and DNS check on the private fleet.
set -Eeuo pipefail

KIP_KUBECONFIG=${KIP_KUBECONFIG:-/etc/kubernetes/admin.conf}
KIP_FLEET_GPU_COUNT=${KIP_FLEET_GPU_COUNT:-1}
namespace=kip-fleet-network-smoke
created=0

kctl() { command kubectl --kubeconfig "$KIP_KUBECONFIG" "$@"; }
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [ "$created" -eq 1 ]; then
        if ! kctl delete namespace "$namespace" --ignore-not-found=true \
            --wait=true --timeout=3m >/dev/null; then
            printf '%s\n' 'Fleet network smoke cleanup failed.' >&2
            status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$#" -eq 0 ] || { printf '%s\n' 'network-smoke accepts no arguments.' >&2; exit 2; }
case "$KIP_FLEET_GPU_COUNT" in
    1 | 2) ;;
    *) printf '%s\n' 'FLEET_GPU_COUNT must be 1 or 2.' >&2; exit 2 ;;
esac
if kctl get namespace "$namespace" >/dev/null 2>&1; then
    printf '%s\n' 'Existing fleet network smoke namespace requires inspection; refusing overwrite.' >&2
    exit 1
fi
nodes=(kip-control kip-gpu-1)
[ "$KIP_FLEET_GPU_COUNT" = 2 ] && nodes+=(kip-gpu-2)
for node in "${nodes[@]}"; do
    ready=$(kctl get node "$node" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}')
    [ "$ready" = True ] || { printf 'Node %s is not Ready.\n' "$node" >&2; exit 1; }
done
created=1
kctl apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: kip-fleet-network-smoke
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/component: fleet-network-smoke
YAML
for node in "${nodes[@]}"; do
    pod="$node-smoke"
    kctl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: $namespace
  labels: {app: $pod}
spec:
  nodeSelector: {kubernetes.io/hostname: $node}
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
  restartPolicy: Never
  containers:
    - name: server
      image: busybox:1.37.0
      command: [sh, -c, 'echo $node >/tmp/index.html; httpd -f -p 8080 -h /tmp']
      ports: [{containerPort: 8080}]
      readinessProbe:
        httpGet: {path: /, port: 8080}
        periodSeconds: 2
---
apiVersion: v1
kind: Service
metadata:
  name: $pod
  namespace: $namespace
spec:
  type: ClusterIP
  selector: {app: $pod}
  ports: [{port: 8080, targetPort: 8080}]
YAML
done

for node in "${nodes[@]}"; do
    kctl -n "$namespace" wait --for=condition=Ready "pod/$node-smoke" --timeout=3m >/dev/null
done
for source_node in "${nodes[@]}"; do
    for destination_node in "${nodes[@]}"; do
        [ "$source_node" = "$destination_node" ] && continue
        destination_pod="$destination_node-smoke"
        destination_ip=$(kctl -n "$namespace" get pod "$destination_pod" -o 'jsonpath={.status.podIP}')
        [ -n "$destination_ip" ] || exit 1
        pod_response=$(kctl -n "$namespace" exec "$source_node-smoke" -- \
            wget -qO- "http://$destination_ip:8080/")
        service_response=$(kctl -n "$namespace" exec "$source_node-smoke" -- \
            wget -qO- "http://$destination_pod.$namespace.svc.cluster.local:8080/")
        [ "$pod_response" = "$destination_node" ] \
            && [ "$service_response" = "$destination_node" ] \
            || { printf 'Cross-node path failed from %s to %s.\n' \
                "$source_node" "$destination_node" >&2; exit 1; }
        printf 'PASS %s -> %s: Pod IP, Service, DNS\n' "$source_node" "$destination_node"
    done
done
