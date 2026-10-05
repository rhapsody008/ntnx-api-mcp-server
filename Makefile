IMAGE    ?= ghcr.io/rhapsody008/ntnx-api-mcp-server
TAG      ?= $(shell git rev-parse --short HEAD)
PORT     ?= 8000
ENV_FILE ?= .env
# Set PLATFORM=linux/amd64 when building on Apple Silicon for an amd64 cluster.
PLATFORM ?=
# Uses the project virtualenv when present.
PYTHON   ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: install test lint build push run-local smoke k8s-apply k8s-logs

install:   ; $(PYTHON) -m pip install -e . && $(PYTHON) -m pip install pytest
test:      ; $(PYTHON) -m pytest -q
lint:      ; $(PYTHON) -m compileall -q src tests
build:     ; docker build $(if $(PLATFORM),--platform $(PLATFORM)) -t $(IMAGE):$(TAG) -t $(IMAGE):latest .
push:      ; docker push $(IMAGE):$(TAG) && docker push $(IMAGE):latest
run-local: ; docker run --rm -p $(PORT):8000 --env-file $(ENV_FILE) $(IMAGE):$(TAG)
smoke:     ; curl -fsS localhost:$(PORT)/healthz && echo && curl -fsS localhost:$(PORT)/readyz && echo
k8s-apply: ; kubectl apply -k deploy/k8s
k8s-logs:  ; kubectl -n nutanix-mcp logs -l app=nutanix-mcp -f
