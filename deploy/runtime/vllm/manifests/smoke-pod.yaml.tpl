apiVersion: v1
kind: Pod
metadata:
  name: kip-runtime-smoke
  namespace: __NAMESPACE__
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/part-of: kube-inference-platform
    app.kubernetes.io/component: runtime-smoke
spec:
  restartPolicy: Never
  securityContext:
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: client
      image: __VLLM_IMAGE__
      imagePullPolicy: IfNotPresent
      command: ["python3", "/opt/kip/smoke/client.py"]
      args:
        - --base-url
        - http://__RESOURCE_NAME__:__SERVICE_PORT__
        - --model
        - __SERVED_MODEL_NAME__
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
        limits:
          cpu: "1"
          memory: 512Mi
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: smoke-client
          mountPath: /opt/kip/smoke
          readOnly: true
  volumes:
    - name: smoke-client
      configMap:
        name: kip-runtime-smoke-client
