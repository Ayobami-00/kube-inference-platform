fullnameOverride: kip-opencost
# This value is the Kubernetes DNS suffix used to build the Prometheus endpoint.
# Cost-series identity remains the stable kip-v01 defaultClusterId below.
clusterName: cluster.local
loglevel: info

kip:
  chartDigest: sha256:534e846fc11b8e7e9967dd07fe808e552395a0985457f0cec6e0715d6c177bbc
  managedBy: kube-inference-platform
  pricingState: priced
  costProfile: __PROFILE_NAME__
  costProfileSha256: __PROFILE_HASH__

plugins:
  enabled: false

service:
  enabled: true
  type: ClusterIP

podLabels:
  kube-inference-platform.io/cost-profile: __PROFILE_NAME__
podAnnotations:
  kube-inference-platform.io/cost-profile-sha256: __PROFILE_HASH__
  kube-inference-platform.io/pricing-effective-from: __EFFECTIVE_FROM__

opencost:
  mcp:
    enabled: false
  exporter:
    defaultClusterId: kip-v01
    image:
      fullImageName: ghcr.io/opencost/opencost:1.121.1@sha256:50052694785db32174332670920faf7b7f55d32a91ba3caec1e9207a58338ff5
      pullPolicy: IfNotPresent
    replicas: 1
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        cpu: "2"
        memory: 2Gi
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
      readOnlyRootFilesystem: true
      runAsNonRoot: true
    prometheusDataSource:
      queryResolutionSeconds: 15
    collectorDataSource:
      enabled: false
    persistence:
      enabled: false
    inferenceCostTracking:
      enabled: true
      modelLabel: kube-inference-platform.io/served-model
      sharedInfraLabel: kube-inference-platform.io/inference-shared
      sharedInfraLabelValue: "true"
      collectionInterval: 1m
  customPricing:
    enabled: true
    configmapName: kip-opencost-pricing
    createConfigmap: true
    provider: custom
    costModel:
      description: "KIP whole-node profile __PROFILE_NAME__ (__PROFILE_HASH__)"
      CPU: 0
      spotCPU: 0
      RAM: 0
      spotRAM: 0
      GPU: __GPU_HOURLY_RATE_USD__
      storage: 0
      zoneNetworkEgress: 0
      regionNetworkEgress: 0
      internetNetworkEgress: 0
  carbonCost:
    enabled: false
  cloudCost:
    enabled: false
  metrics:
    serviceMonitor:
      enabled: true
      additionalLabels:
        release: kip-monitoring
      namespace: kip-observability
      scrapeInterval: 30s
      scrapeTimeout: 10s
      honorLabels: true
  prometheus:
    external:
      enabled: false
    internal:
      enabled: true
      serviceName: kip-monitoring-prometheus
      namespaceName: kip-observability
      port: 9090
      scheme: http
  ui:
    enabled: false
