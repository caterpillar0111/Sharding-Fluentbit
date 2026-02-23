.PHONY: cluster-up cluster-down build load deploy-vector deploy-apps deploy-all logs-vector logs-tool status clean

CLUSTER_NAME := log-poc
IMAGE_NAME   := test-app
IMAGE_TAG    := latest

## ── Cluster ────────────────────────────────────────────────────────────────

cluster-up:
	kind create cluster --config kind-config.yaml

cluster-down:
	kind delete cluster --name $(CLUSTER_NAME)

## ── Image ──────────────────────────────────────────────────────────────────

build:
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) ./test-app

load: build
	kind load docker-image $(IMAGE_NAME):$(IMAGE_TAG) --name $(CLUSTER_NAME)

## ── Deploy ─────────────────────────────────────────────────────────────────

deploy-vector:
	kubectl apply -f vector-config/pvc-0.yaml
	kubectl apply -f vector-config/pvc-1.yaml
	kubectl apply -f vector-config/configmap.yaml
	kubectl apply -f vector-config/service-0.yaml
	kubectl apply -f vector-config/service-1.yaml
	kubectl apply -f vector-config/deployment-0.yaml
	kubectl apply -f vector-config/deployment-1.yaml
	kubectl rollout status deployment/vector-0
	kubectl rollout status deployment/vector-1

deploy-apps: load
	kubectl apply -f fluentbit-config/configmap.yaml
	kubectl apply -f test-app/deployment.yaml
	kubectl rollout status deployment/tool-001
	kubectl rollout status deployment/tool-002

deploy-all: deploy-vector deploy-apps

## ── Observe ────────────────────────────────────────────────────────────────

# Stream Vector logs (usage: make logs-vector SHARD=0)
SHARD ?= 0
logs-vector:
	kubectl logs -f deployment/vector-$(SHARD) -c vector

# Stream a specific tool's Fluent Bit sidecar  (usage: make logs-tool TOOL=tool-001)
TOOL ?= tool-001
logs-tool:
	kubectl logs -f deployment/$(TOOL) -c fluent-bit

# Show pod status
status:
	kubectl get pods -o wide

# Peek at files written inside Vector's PVC (usage: make browse-pvc SHARD=0)
browse-pvc:
	kubectl exec deployment/vector-$(SHARD) -- find /logs -type f | sort

## ── Cleanup ────────────────────────────────────────────────────────────────

clean:
	kubectl delete -f test-app/deployment.yaml --ignore-not-found
	kubectl delete -f fluentbit-config/configmap.yaml --ignore-not-found
	kubectl delete -f vector-config/ --ignore-not-found
