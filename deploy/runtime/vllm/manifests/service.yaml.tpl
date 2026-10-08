apiVersion: v1
kind: Service
metadata:
  name: __RESOURCE_NAME__
  namespace: __NAMESPACE__
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/part-of: kube-inference-platform
    app.kubernetes.io/component: runtime
    app.kubernetes.io/name: vllm
    app.kubernetes.io/instance: __PROFILE_NAME__
    kube-inference-platform.io/runtime: vllm
    kube-inference-platform.io/model-profile: __PROFILE_NAME__
    kube-inference-platform.io/served-model: __SERVED_MODEL_NAME__
    kube-inference-platform.io/model-revision: __MODEL_REVISION__
  annotations:
    kube-inference-platform/runtime-version: __VLLM_VERSION__
    kube-inference-platform/profile: __PROFILE_NAME__
    kube-inference-platform/profile-sha256: __PROFILE_HASH__
    kube-inference-platform/model-revision: __MODEL_REVISION__
    kube-inference-platform/image-digest: __IMAGE_DIGEST__
    kube-inference-platform.io/model-id: __MODEL_ID__
    prometheus.io/scrape: "true"
    prometheus.io/path: /metrics
    prometheus.io/port: "__SERVICE_PORT__"
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: vllm
    app.kubernetes.io/instance: __PROFILE_NAME__
  ports:
    - name: http
      protocol: TCP
      port: __SERVICE_PORT__
      targetPort: http
      appProtocol: http
