# Lambda campaign overlay for the pinned kube-prometheus-stack chart.
prometheus:
  prometheusSpec:
    externalLabels:
      cluster: kip-lambda
    tolerations:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
    nodeSelector:
      kubernetes.io/hostname: __CONTROL_NODE__
grafana:
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
  nodeSelector:
    kubernetes.io/hostname: __CONTROL_NODE__
alertmanager:
  alertmanagerSpec:
    tolerations:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
    nodeSelector:
      kubernetes.io/hostname: __CONTROL_NODE__
prometheusOperator:
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
  nodeSelector:
    kubernetes.io/hostname: __CONTROL_NODE__
