# Kubernetes deployment — Nutanix V4 API MCP Server

> **Scope:** Running `nutanix-mcp serve-http` in a Kubernetes cluster, building and publishing the image, and verifying it with a canary.
> Image internals: [deployment guide §3 Docker](./deployment.md#3-docker). All settings: [configuration reference](./configuration.md).

---

## 1. What gets deployed

Manifests live in [`deploy/k8s/`](../deploy/k8s/) and are applied with Kustomize.

| File | Object | Notes |
|---|---|---|
| `namespace.yaml` | Namespace `nutanix-mcp` | |
| `configmap.yaml` | ConfigMap `nutanix-mcp-config` | Non-secret settings (table in §4) |
| `secret.example.yaml` | Secret `nutanix-mcp-secrets` | **Template only.** Copy to `secret.yaml` (gitignored) and apply it yourself |
| `pvc.yaml` | PVC `nutanix-mcp-artifacts` (1Gi, RWO) | Keeps downloaded specs across restarts |
| `deployment.yaml` | Deployment `nutanix-mcp` | 1 replica, non-root, read-only root FS, all capabilities dropped |
| `service.yaml` | Service `nutanix-mcp` (ClusterIP :8000) | In-cluster URL: `http://nutanix-mcp.nutanix-mcp.svc:8000/mcp` |
| `ingress.example.yaml` | Ingress (optional) | Only for clients outside the cluster, and only with TLS |

Pod startup sequence:

1. `docker/entrypoint.sh` runs `nutanix-mcp init` (when `INIT_ON_START=true`). The first start downloads specs into the PVC. Later starts skip files that already exist.
2. `serve-http` starts listening. `/healthz` returns 200 immediately.
3. Specs are parsed in the background. `/readyz` returns 503 until operations are loaded, then 200 with `operation_count`.

The `startupProbe` allows up to 10 minutes for step 1. Liveness checks `/healthz` and readiness checks `/readyz`.

---

## 2. Build and push the image

### 2.1 With GitHub Actions (recommended)

[`.github/workflows/container.yml`](../.github/workflows/container.yml) builds the image, smoke-tests it, and pushes it to `ghcr.io/<owner>/<repo>`:

| Trigger | Tags published |
|---|---|
| Push to `main` | `:latest`, `:<short-sha>` |
| Push of a `v*` tag | `:latest`, `:<short-sha>`, `:<tag>` |
| Manual run (**Actions → Build and publish container → Run workflow**) | `:latest` (unless `tag_latest` is unchecked), `:<short-sha>` |
| Pull request | none — builds and smoke-tests only |

`:latest` is always pushed first, so it is the tag `deploy/k8s/deployment.yaml` pulls. The workflow authenticates with the built-in `GITHUB_TOKEN`, so no registry secret is needed.

Before the push, the job runs `pytest`, then starts the built image with the same hardening as the Deployment and `INIT_ON_START=false`. It fails the build unless `/readyz` reports at least one loaded operation (which proves the baked-in specs work offline) and `/mcp` returns 401 without a bearer token. A broken image never reaches `:latest`.

Two manual-run inputs are available: `tag_latest` (uncheck to publish only the sha tag) and `bake_specs` (uncheck for an offline-style build with no bundled specs).

> The GHCR package is **private** on first publish. Either make it public in the package settings, or create an image pull secret in the namespace and add `imagePullSecrets` to the Deployment.

### 2.2 Locally with make

```bash
# From the repository root
make test
make build PLATFORM=linux/amd64          # omit PLATFORM when the build host matches the cluster
echo "$GHCR_TOKEN" | docker login ghcr.io -u rhapsody008 --password-stdin
make push                                # pushes :<git-sha> and :latest
```

`make build` bakes the latest-release specs into the image as a fallback, which needs internet access during the build. Use `docker build --build-arg BAKE_SPECS=false ...` for an offline build.

To deploy a specific build, pin it in `deploy/k8s/kustomization.yaml`:

```yaml
images:
  - name: ghcr.io/rhapsody008/ntnx-api-mcp-server
    newTag: <git-sha>
```

If the GHCR package is private, create an image pull secret in the namespace and add `imagePullSecrets` to the Deployment.

---

## 3. Configure

### 3.1 Secret

```bash
kubectl apply -f deploy/k8s/namespace.yaml
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml   # gitignored
# Edit: set PC_API_KEY (or PC_USERNAME/PC_PASSWORD) and MCP_AUTH_TOKEN
openssl rand -base64 32                                     # value for MCP_AUTH_TOKEN
kubectl apply -f deploy/k8s/secret.yaml
```

Equivalent without a file:

```bash
kubectl -n nutanix-mcp create secret generic nutanix-mcp-secrets \
  --from-literal=PC_API_KEY='<api-key>' \
  --from-literal=MCP_AUTH_TOKEN="$(openssl rand -base64 32)"
```

### 3.2 ConfigMap

Edit `deploy/k8s/configmap.yaml`. At minimum, set `PC_HOST` and `PC_INSECURE`. Keep `READ_ONLY_MODE: "true"` until the canary in §7 passes.

---

## 4. Environment variables

| Variable | Source | Suggested value | Notes |
|---|---|---|---|
| `PC_HOST` | ConfigMap | `171.16.0.10` | IP or FQDN, no scheme |
| `PC_PORT` | ConfigMap | `9440` | Port 9440 means https; any other port means http |
| `PC_API_KEY` | Secret | — | Preferred. Mutually exclusive with username/password |
| `PC_USERNAME` / `PC_PASSWORD` | Secret | — | Only if not using an API key |
| `PC_INSECURE` | ConfigMap | `true` for a self-signed lab PC | Keep `false` anywhere real |
| `READ_ONLY_MODE` | ConfigMap | `true` first, then `false` | Writes are blocked server-side until you flip it |
| `STRICT_PARAMS` | ConfigMap | `true` | Unknown keys become errors instead of silent drops |
| `AUTO_ETAG` | ConfigMap | `true` | The server fetches `If-Match` itself |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` / `MCP_HTTP_PATH` | ConfigMap | `0.0.0.0` / `8000` / `/mcp` | |
| `MCP_STATELESS` | ConfigMap | `true` | Needed for more than 1 replica without sticky sessions |
| `MCP_AUTH_TOKEN` | Secret | random 32+ bytes | Strongly recommended, since this server can write to Prism |
| `ARTIFACTS_DIR` | Image default | `/data/artifacts` | PVC-backed so specs survive restarts |
| `LOG_DIR` | Image default | `/data/logs` | emptyDir. Logs also go to stderr for `kubectl logs` |
| `LOG_LEVEL` / `LOG_FORMAT` | ConfigMap | `INFO` / `json` | |
| `INIT_ON_START` | ConfigMap | `true` | Runs `nutanix-mcp init` before serving |
| `NAMESPACE_OVERRIDE_LIST` | ConfigMap (optional) | e.g. `vmm,prism,clustermgmt,networking` | Fewer namespaces means faster startup and smaller tool lists |
| `NAMESPACE_SOURCE_URL` | ConfigMap (optional) | default | Only if mirroring developers.nutanix.com |

**Network egress:** the pod must reach Prism Central on `:9440`. `init` also needs `developers.nutanix.com:443`, unless you rely on specs already in the PVC or baked into the image. If you use NetworkPolicies, allow both.

**Scaling out:** set `MCP_STATELESS=true` and switch the artifacts volume to `ReadWriteMany` or `emptyDir` before raising `replicas`. The default PVC is `ReadWriteOnce`, which is also why the Deployment uses the `Recreate` strategy.

---

## 5. Apply and verify

```bash
make k8s-apply                                  # kubectl apply -k deploy/k8s
kubectl -n nutanix-mcp rollout status deploy/nutanix-mcp --timeout=10m
make k8s-logs                                   # follow init + startup logs

kubectl -n nutanix-mcp port-forward svc/nutanix-mcp 8000:8000 &
make smoke                                      # {"status":"ok"} then {"status":"ready","operation_count":N}

# The MCP path rejects requests without the token
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/mcp          # 401
```

---

## 6. Troubleshooting

### Pod logs `Read-only file system` and never becomes ready

Older builds crashed at startup when `/data/logs` was not writable, because the per-restart log file could not be created. Current builds degrade instead: file logging is skipped with a `file_logging_disabled` warning, and logs continue to stderr for `kubectl logs`. Rebuild from this branch if you see that traceback.

### Startup takes minutes before the port opens

With `INIT_ON_START=true` and no writable volume at `/data/artifacts`, the entrypoint used to download all 20 specs and fail to write every one — roughly two wasted minutes per restart. It now detects the unwritable directory and serves the bundled specs immediately. Either outcome is safe, but for a pod with no artifacts volume set `INIT_ON_START: "false"` to make the intent explicit.

### Choosing volumes

| Setup | Behavior |
|---|---|
| No volumes, `readOnlyRootFilesystem: true` | Bundled specs only, stderr logs, ready in seconds. Simplest option. |
| emptyDir at `/data/artifacts` | Specs re-downloaded on every pod start (adds minutes). |
| PVC at `/data/artifacts` (the default here) | Specs downloaded once, reused across restarts. |

A writable `/data/logs` is never required. `kubectl logs` reads stderr either way.

---

## 7. Canary verification

### 7.1 MCP Inspector over HTTP

```bash
npx @modelcontextprotocol/inspector
```

In the Inspector UI, choose transport **Streamable HTTP**, URL `http://localhost:8000/mcp`, and add the header `Authorization: Bearer <MCP_AUTH_TOKEN>`. Then call `vmm_execute`:

| Check | Arguments | Expected |
|---|---|---|
| Bucket form | `{"operation": "ahv_listVms", "query_params": {"$filter": "startswith(name, 'zzz-nomatch')"}}` | `ok: true`, zero VMs |
| Legacy flat key | `{"operation": "ahv_listVms", "$filter": "startswith(name, 'zzz-nomatch')"}` | `ok: true`, zero VMs |
| Unknown key | `{"operation": "ahv_listVms", "query_params": {"$fliter": "x"}}` | `isError`, `code: unknown_parameter`, accepted names listed |

If the first two checks return your whole VM list, the filter is being dropped. Check that the server runs this build and that the client re-synced its tool list.

### 7.2 Open WebUI

1. In Open WebUI, add an MCP tool server (Streamable HTTP) pointing at `http://nutanix-mcp.nutanix-mcp.svc:8000/mcp` with bearer auth set to `MCP_AUTH_TOKEN`.
2. Re-sync the tools so Open WebUI picks up the new `path_params` / `query_params` / `headers` properties.
3. Rerun the three canary checks from §7.1 through a chat prompt, for example: *"List AHV VMs whose name starts with zzz-nomatch."* The answer must be zero VMs, not the full inventory.

### 7.3 Write path with server-side ETag

1. Set `READ_ONLY_MODE: "false"` and `AUTO_ETAG: "true"` in the ConfigMap. Run `make k8s-apply`, then `kubectl -n nutanix-mcp rollout restart deploy/nutanix-mcp`.
2. Ask the model to associate a test category with one test VM. It should call the associate-categories action with only `path_params` and `request_body`, with no GET of its own and no `If-Match`.
3. In `make k8s-logs`, confirm `event=auto_etag_injected operation=...` followed by the write's `event=api_call`.
   AUTO_ETAG only fires for operations whose spec declares `If-Match`. The vmm v4.3 spec declares it on `esxi_associateCategories` but **not** on `ahv_associateCategories`. If the AHV call succeeds with no `auto_etag_injected` line, Prism Central did not require an ETag. If it fails with HTTP 428 (Precondition Required), the spec is incomplete: pass `headers: {"If-Match": "<_etag>"}` explicitly, which the server accepts and forwards for any non-GET operation.
4. Verify with a GET of the VM (or `$filter` on the category) that the category is attached.

`AUTO_ETAG` reads the ETag and writes back immediately, so it does not protect against a concurrent change made between the model's earlier read and its write. For category association that is harmless. Set `AUTO_ETAG: "false"` if you need that protection for full-body PUTs.
