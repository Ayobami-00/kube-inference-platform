apiVersion: apps/v1
kind: Deployment
metadata:
  name: __RESOURCE_NAME__
  namespace: __NAMESPACE__
  labels:
    app.kubernetes.io/managed-by: kube-inference-platform
    app.kubernetes.io/part-of: kube-inference-platform
    app.kubernetes.io/component: runtime
    app.kubernetes.io/name: vllm
    app.kubernetes.io/instance: __PROFILE_NAME__
  annotations:
    kube-inference-platform/runtime-version: __VLLM_VERSION__
    kube-inference-platform/profile: __PROFILE_NAME__
    kube-inference-platform/profile-sha256: __PROFILE_HASH__
    kube-inference-platform/model-revision: __MODEL_REVISION__
    kube-inference-platform/image-digest: __IMAGE_DIGEST__
spec:
  replicas: __REPLICAS__
  strategy:
    type: Recreate
  progressDeadlineSeconds: 2700
  minReadySeconds: 10
  selector:
    matchLabels:
      app.kubernetes.io/name: vllm
      app.kubernetes.io/instance: __PROFILE_NAME__
  template:
    metadata:
      labels:
        app.kubernetes.io/managed-by: kube-inference-platform
        app.kubernetes.io/part-of: kube-inference-platform
        app.kubernetes.io/component: runtime
        app.kubernetes.io/name: vllm
        app.kubernetes.io/instance: __PROFILE_NAME__
      annotations:
        kube-inference-platform/profile-sha256: __PROFILE_HASH__
    spec:
      terminationGracePeriodSeconds: 120
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: vllm
          image: __VLLM_IMAGE__
          imagePullPolicy: IfNotPresent
          command: ["vllm", "serve"]
          args:
            - __MODEL_ID__
            - --revision
            - __MODEL_REVISION__
            - --tokenizer-revision
            - __MODEL_REVISION__
            - --served-model-name
            - __SERVED_MODEL_NAME__
            - --host
            - 0.0.0.0
            - --port
            - "__SERVICE_PORT__"
            - --dtype
            - __DTYPE__
            - --tensor-parallel-size
            - "__TENSOR_PARALLEL_SIZE__"
            - --max-model-len
            - "__MAX_MODEL_LENGTH__"
            - --gpu-memory-utilization
            - "__GPU_MEMORY_UTILIZATION__"
            - --max-num-seqs
            - "__MAX_NUMBER_OF_SEQUENCES__"
            - --seed
            - "__SEED__"
          env:
            - name: HF_HOME
              value: /var/cache/kip/huggingface
__HF_TOKEN_ENV__
          ports:
            - name: http
              containerPort: __SERVICE_PORT__
              protocol: TCP
          resources:
            requests:
              cpu: __CPU_REQUEST__
              memory: __MEMORY_REQUEST__
              ephemeral-storage: __EPHEMERAL_REQUEST__
              nvidia.com/gpu: "__GPU_REQUEST__"
            limits:
              cpu: __CPU_LIMIT__
              memory: __MEMORY_LIMIT__
              ephemeral-storage: __EPHEMERAL_LIMIT__
              nvidia.com/gpu: "__GPU_LIMIT__"
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          startupProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 180
          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 15
            timeoutSeconds: 3
            failureThreshold: 4
          volumeMounts:
            - name: model-cache
              mountPath: /var/cache/kip
            - name: shared-memory
              mountPath: /dev/shm
      volumes:
        - name: model-cache
          hostPath:
            path: __CACHE_PATH__
            type: Directory
        - name: shared-memory
          emptyDir:
            medium: Memory
            sizeLimit: __SHARED_MEMORY_SIZE__
