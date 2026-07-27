# MCP-HUB

An AI-powered DevOps automation platform. [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) orchestrates a set of MCP servers (GitHub, Jira, Slack, Azure, Kubernetes) to investigate infrastructure, answer questions in Slack, and — proactively, without being asked — detect problems, find the root cause in your code, and open a pull request to fix it.

```
                         ┌──────────────────────────┐
   Slack (@mention) ───▶ │      slack-listener      │
                         └────────────┬─────────────┘
                                      │
   watcher (scheduled) ──────────────▶│
   • Azure App Service                │
   • Kubernetes pods/events           ▼
   • GitHub Dependabot         ┌─────────────┐        ┌──────────────────────┐
   alerts                      │   Holmes    │◀──────▶│ MCP servers:          │
                                │ (AI brain)  │        │ GitHub · Jira · Slack │
                                └──────┬──────┘        │ Azure API · Grafana   │
                                       │                │ Kubernetes            │
                                       ▼                └──────────────────────┘
                        ┌───────────────────────────┐
                        │ Slack alert → investigate  │
                        │ → branch → commit → PR     │
                        └───────────────────────────┘

   Frontend (React) ──▶ control-plane (Flask) ──▶ .env + docker compose restart
```

## What it does

**Reactive** — `@mention` Holmes in Slack and ask anything: *"list open GitHub PRs"*, *"are all pods healthy?"*, *"show Jira tickets in progress"*. Holmes picks the right tools and answers.

**Proactive** — `watcher` polls on a schedule and only calls the LLM when something's actually wrong:
- **Azure App Service** — app state, HTTP 5xx rate, and real exception tracebacks pulled from logs
- **Kubernetes** — `CrashLoopBackOff`, `ImagePullBackOff`, `OOMKilled`, high pod restart counts, recent Warning events
- **Security** — open GitHub Dependabot alerts at or above a configurable severity

When something's found: Slack alert → Holmes investigates the repo for a root cause → if fixable, opens a PR (branch + commit + description) → posts the PR link back to the same Slack thread for human review. Nothing merges automatically.

**Control plane + frontend** — a small React app and Flask API let you manage connector credentials (GitHub, Jira, Slack, Grafana, Azure) without hand-editing `.env`, and restart only the affected containers when they change.

## Project structure

```
MCP-HUB/
├── docker-compose.yaml
├── config.yaml                  # Holmes model + toolset config
├── holmes-custom/                # Holmes image: Azure CLI, kubectl-safe az wrapper
├── slack-listener/                # Slack @mention → Holmes bridge
├── watcher/                       # Proactive monitoring (Azure, K8s, Security)
├── watcher-data/                  # Persisted dedup state (watcher_state.json)
└── backend/                       # control-plane API + React frontend
    ├── app.py                     # Flask: writes .env, restarts affected services
    ├── App.jsx / main.jsx         # Connectors / Ask Holmes / Dashboard UI
    └── docker-compose.yaml
```

## Prerequisites

- Docker Desktop, with **Kubernetes enabled** (Settings → Kubernetes → Enable Kubernetes) if you want the K8s workflow
- A Gemini API key ([ai.google.dev](https://ai.google.dev)) — check your account's actual available model list before configuring one, several model names look plausible but aren't enabled on every tier
- A GitHub Personal Access Token with repo access, plus `security_events` (classic) or "Dependabot alerts: read" (fine-grained) scope if you want the security-scanning check
- A Slack app with Socket Mode enabled — you need **both** a bot token (`xoxb-...`, from OAuth & Permissions) and an app-level token (`xapp-...`, from the separate App-Level Tokens section, scope `connections:write`)
- An Azure subscription + service principal (`az ad sp create-for-rbac`) if using the Azure workflow

## Quick setup

**1. Environment variables** — create `.env` in the project root:

```bash
GEMINI_API_KEY=...
GITHUB_TOKEN=ghp_...
GITHUB_REPO=your-org/your-repo          # repo Holmes opens PRs against

SLACK_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=your-alerts-channel

AZURE_APP_SERVICE=your-app-name
AZURE_RESOURCE_GROUP=your-resource-group
AZURE_SUBSCRIPTION_ID=...
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...

K8S_ENABLED=true
K8S_NAMESPACE=default

SECURITY_ENABLED=true
SECURITY_MIN_SEVERITY=high
```

**2. Kubernetes setup**

- Enable Kubernetes in Docker Desktop (or point at any cluster you already have access to) and confirm it's actually running: `kubectl get nodes`.
- `watcher` and `holmes` both need your kubeconfig mounted read-only:
  ```yaml
  volumes:
    - ~/.kube/config:/tmp/.kube/config:ro
  ```
  At startup, both containers copy this file and rewrite `localhost`/`127.0.0.1` to `host.docker.internal` — a container can't reach the host cluster via `localhost` (that resolves to itself), so this rewrite is required for Docker Desktop's local cluster to be reachable from inside the containers.
- No further action needed — `watcher` polls `kubectl get pods` / `kubectl get events` in `K8S_NAMESPACE` on its own schedule.

**3. Start the stack**

```bash
docker compose up -d --build
docker compose ps        # everything should show Up (holmes: Up (healthy))
```

**4. Control plane + frontend** (optional, for credential management via UI)

```bash
cd backend
pip install -r requirements.txt
export CONTROL_PLANE_TOKEN=<pick-a-token>
python app.py             # serves on :4000

# separately:
npm install && npm run dev   # serves on :5173
```

## Testing the workflow

A minimal, intentionally-buggy FastAPI app is here for exercising both the Azure and Kubernetes paths end-to-end:

**[github.com/PriyanshiGupta1701/mcp-hub-test](https://github.com/PriyanshiGupta1701/mcp-hub-test)**

It exposes `/items/{id}` (unhandled `KeyError` on unknown ids) and `/divide/{n}` (`ZeroDivisionError` at `n=5`) — real unhandled exceptions, not synthetic errors, so both the log-scraping and the fix/PR steps have something genuine to work with.

**Testing Azure:** deploy it to your `AZURE_APP_SERVICE` (see the repo's own README for the GitHub Actions deploy setup — including the Azure basic-auth toggle, or the OIDC `azure/login` alternative, and the `SCM_DO_BUILD_DURING_DEPLOYMENT=true` app setting it needs), then:
```bash
curl https://<your-app>.azurewebsites.net/items/99
curl https://<your-app>.azurewebsites.net/divide/5
```

**Testing Kubernetes:** build and deploy the same app to your local cluster:
```bash
docker build -t mcp-hub-test:local .
kubectl create deployment mcp-hub-test --image=mcp-hub-test:local -n default
kubectl expose deployment mcp-hub-test --port=80 --target-port=8000 -n default
kubectl port-forward svc/mcp-hub-test 8080:80 -n default
curl http://localhost:8080/items/99
```
To actually exercise the *pod-health* side of the check (not just app-level errors), try something that kills the container, e.g. temporarily setting the image to a bad tag (`ImagePullBackOff`) or lowering `resources.limits.memory` until it `OOMKilled`s.

Either way, wait for the next `watcher` poll cycle (or `docker compose restart watcher` to force one) and check Slack for the alert thread.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `POLL_INTERVAL_SEC` | `300` | How often `watcher` runs all checks |
| `CHECK_WINDOW_MINUTES` | `15` | Lookback window for Azure metrics / K8s events |
| `LOG_WINDOW_MINUTES` | `20` | Separate lookback window for Azure log scanning |
| `HTTP_5XX_THRESHOLD` | `1` | Minimum 5xx count in-window to count as an issue |
| `K8S_POD_RESTART_THRESHOLD` | `5` | Restart count that counts as "high" |
| `SECURITY_MIN_SEVERITY` | `high` | Minimum Dependabot severity to escalate (`low`/`medium`/`high`/`critical`) |
| `SECURITY_MAX_NEW_ALERTS_PER_CYCLE` | `1` | Caps how many new vulnerabilities get escalated per poll, to avoid a burst |
| `LITELLM_NUM_RETRIES` | `3` (we set `8`) | How many times litellm auto-retries on a real 429 before giving up |

## Current status / known limitations

| Item | Status |
|---|---|
| Azure monitoring + auto-fix PR | ✅ Working |
| Kubernetes monitoring + auto-fix PR | ✅ Working |
| Security (Dependabot) monitoring | ✅ Working |
| Slack chat (`@mention`) | ✅ Working |
| Frontend + control-plane credential management | ✅ Working |
| Grafana MCP | ⚠️ 403 auth — needs a fresh service account token |
| k8s-remediation MCP | ⚠️ 404 — SSE path mismatch, unused (superseded by `watcher`'s own kubectl checks) |
| Fix/PR step on `gemini-3.1-flash-lite` | ⚠️ Occasionally returns an empty response on complex prompts — mitigated by splitting investigate/execute into two smaller calls, not fully eliminated |

## How it stays cheap

Every check is designed to spend zero LLM calls when nothing's wrong: `watcher` calls `az`/`kubectl`/the GitHub REST API directly for raw evidence first, and only invokes Holmes once that evidence suggests a real problem. The one exception is security scanning, which skips the LLM entirely even *with* an issue found — Dependabot's data is already structured enough that Holmes only gets involved for the actual code investigation and PR, not to interpret whether something's wrong in the first place.
