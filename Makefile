.PHONY: cluster-up cluster-down build load deploy-vector deploy-apps deploy-all logs-vector logs-tool status clean build-log-reader load-log-reader deploy-log-reader query-log

CLUSTER_NAME    := log-poc
IMAGE_NAME      := test-app
IMAGE_TAG       := latest
VECTOR_NS       := ea-tapinfra
HELM_RELEASE    := vector

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

build-log-reader:
	docker build -t log-reader:latest ./log-reader

load-log-reader: build-log-reader
	kind load docker-image log-reader:latest --name $(CLUSTER_NAME)

## ── Deploy ─────────────────────────────────────────────────────────────────

deploy-vector:
	kubectl create namespace $(VECTOR_NS) --dry-run=client -o yaml | kubectl apply -f -
	kubectl label namespace default vector-access=true --overwrite
	helm upgrade --install $(HELM_RELEASE) ./helm/vector --set namespace=$(VECTOR_NS)
	kubectl rollout status statefulset/vector -n $(VECTOR_NS)

deploy-apps: load
	kubectl apply -f fluentbit-config/configmap.yaml
	kubectl apply -f test-app/deployment.yaml
	kubectl rollout status deployment/tool-001
	kubectl rollout status deployment/tool-002

deploy-all: deploy-vector deploy-apps

# Build log-reader image, load to kind, and helm upgrade (usage: make deploy-log-reader)
deploy-log-reader: load-log-reader
	helm upgrade $(HELM_RELEASE) ./helm/vector
	kubectl rollout status statefulset/vector -n $(VECTOR_NS)

# Query log via log-reader API (usage: make query-log TOOL=tool-001)
query-log:
	kubectl port-forward -n $(VECTOR_NS) vector-0 8080:8080 &
	sleep 1
	curl -s "http://localhost:8080/logs?toolid=$(TOOL)" | python3 -m json.tool
	kill %1 2>/dev/null || true

## ── Observe ────────────────────────────────────────────────────────────────

# Stream Vector logs (usage: make logs-vector SHARD=0)
SHARD ?= 0
logs-vector:
	kubectl logs -f statefulset/vector --pod=$(HELM_RELEASE)-$(SHARD) -n $(VECTOR_NS)

# Stream a specific tool's Fluent Bit sidecar  (usage: make logs-tool TOOL=tool-001)
TOOL ?= tool-001
logs-tool:
	kubectl logs -f deployment/$(TOOL) -c fluent-bit

# Show pod status
status:
	@echo "=== apps (default) ===" && kubectl get pods -o wide
	@echo "=== vector ($(VECTOR_NS)) ===" && kubectl get pods -n $(VECTOR_NS) -o wide

# Peek at files written inside Vector's PVC (usage: make browse-pvc SHARD=0)
browse-pvc:
	kubectl exec -n $(VECTOR_NS) vector-$(SHARD) -- find /logs -type f | sort

## ── Cleanup ────────────────────────────────────────────────────────────────

clean:
	kubectl delete -f test-app/deployment.yaml --ignore-not-found
	kubectl delete -f fluentbit-config/configmap.yaml --ignore-not-found
	helm uninstall $(HELM_RELEASE) -n $(VECTOR_NS) --ignore-not-found 2>/dev/null || true
	kubectl delete namespace $(VECTOR_NS) --ignore-not-found
