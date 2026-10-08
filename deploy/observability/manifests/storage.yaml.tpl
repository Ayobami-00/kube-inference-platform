apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: kip-local-static
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/part-of: kube-inference-platform
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: kip-prometheus
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    kube-inference-platform.io/volume: prometheus
spec:
  capacity:
    storage: 30Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: kip-local-static
  local:
    path: __STORAGE_ROOT__/prometheus
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [__NODE_NAME__]
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: kip-alertmanager
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    kube-inference-platform.io/volume: alertmanager
spec:
  capacity:
    storage: 2Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: kip-local-static
  local:
    path: __STORAGE_ROOT__/alertmanager
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [__NODE_NAME__]
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: kip-grafana
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    kube-inference-platform.io/volume: grafana
spec:
  capacity:
    storage: 2Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: kip-local-static
  claimRef:
    namespace: kip-observability
    name: kip-grafana
  local:
    path: __STORAGE_ROOT__/grafana
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [__NODE_NAME__]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: kip-grafana
  namespace: kip-observability
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    kube-inference-platform.io/volume: grafana
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: kip-local-static
  volumeName: kip-grafana
  resources:
    requests:
      storage: 2Gi
