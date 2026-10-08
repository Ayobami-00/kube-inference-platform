#!/usr/bin/env bash

# Read-only platform and storage prerequisites.

KIP_OBSERVABILITY_STORAGE_MARKER=".kip-managed-v1"
KIP_OBSERVABILITY_STORAGE_MARKER_CONTENT="kube-inference-platform observability storage v1"

kip_observability_platform_ready() {
    kip_observability_run_cluster_check >/dev/null \
        && kip_observability_run_gpu_check >/dev/null \
        && kip_observability_run_runtime_check >/dev/null \
        && kip_observability_single_ready_node
}

kip_observability_host_storage_ready() {
    local directory first_entry marker marker_content marker_source

    kip_acquire_privileges || return 1
    marker="$KIP_OBSERVABILITY_STORAGE_ROOT/$KIP_OBSERVABILITY_STORAGE_MARKER"
    if kip_run_privileged test -e "$KIP_OBSERVABILITY_STORAGE_ROOT"; then
        kip_run_privileged test -d "$KIP_OBSERVABILITY_STORAGE_ROOT" || return 1
        ! kip_run_privileged test -L "$KIP_OBSERVABILITY_STORAGE_ROOT" || return 1
        if kip_run_privileged test -e "$marker"; then
            kip_run_privileged test -f "$marker" || return 1
            ! kip_run_privileged test -L "$marker" || return 1
            marker_content=$(kip_run_privileged sed -n 'p' "$marker" 2>/dev/null) \
                || return 1
            [ "$marker_content" = "$KIP_OBSERVABILITY_STORAGE_MARKER_CONTENT" ] \
                || return 1
        else
            # A process can stop after mkdir but before installing the marker.
            # Adopt only that uniquely safe crash state: an empty, non-symlink
            # directory. Any payload remains foreign and is never overwritten.
            first_entry=$(kip_run_privileged find "$KIP_OBSERVABILITY_STORAGE_ROOT" \
                -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null) || return 1
            [ -z "$first_entry" ] || return 1
            marker_source="$KIP_WORK_DIR/observability-storage-marker"
            printf '%s\n' "$KIP_OBSERVABILITY_STORAGE_MARKER_CONTENT" > "$marker_source"
            kip_install_file "$marker_source" "$marker" 0444 || return 1
        fi
    else
        kip_make_directory "$KIP_OBSERVABILITY_STORAGE_ROOT" 0755 || return 1
        marker_source="$KIP_WORK_DIR/observability-storage-marker"
        printf '%s\n' "$KIP_OBSERVABILITY_STORAGE_MARKER_CONTENT" > "$marker_source"
        kip_install_file "$marker_source" "$marker" 0444 || return 1
    fi
    for directory in prometheus alertmanager grafana; do
        kip_make_directory "$KIP_OBSERVABILITY_STORAGE_ROOT/$directory" 0755 || return 1
    done
}

kip_observability_grafana_secret_ready() {
    local owner password password_encoded secret

    if kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get secret kip-grafana-admin >/dev/null 2>&1; then
        secret=$(kip_kubectl -n "$KIP_OBSERVABILITY_NAMESPACE" get secret \
            kip-grafana-admin -o json 2>/dev/null) || return 1
        owner=$(printf '%s' "$secret" | kip_exec jq -er \
            '.metadata.labels["app.kubernetes.io/managed-by"] // ""') || return 1
        [ "$owner" = kube-inference-platform ] \
            && printf '%s' "$secret" | kip_exec jq -e '
                (.data["admin-user"] | type == "string" and length > 0)
                and (.data["admin-password"] | type == "string" and length > 0)
            ' >/dev/null
        return
    fi
    password=$(kip_exec openssl rand -base64 32 | command tr -d '\n') || return 1
    [ -n "$password" ] || return 1
    password_encoded=$(printf '%s' "$password" | kip_exec openssl base64 -A) || return 1
    # Feed the Secret manifest over stdin: neither the cleartext password nor
    # its encoded form is exposed in kubectl's process arguments.
    printf '%s\n' \
        'apiVersion: v1' \
        'kind: Secret' \
        'metadata:' \
        '  name: kip-grafana-admin' \
        "  namespace: $KIP_OBSERVABILITY_NAMESPACE" \
        '  labels:' \
        '    app.kubernetes.io/managed-by: kube-inference-platform' \
        '    app.kubernetes.io/part-of: kube-inference-platform' \
        'type: Opaque' \
        'data:' \
        '  admin-user: YWRtaW4=' \
        "  admin-password: $password_encoded" \
        | kip_kubectl apply -f - >/dev/null
}
