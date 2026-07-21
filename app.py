"""
control-plane/app.py
---------------------
Small authenticated service that lets the frontend update connector
credentials (GitHub, Jira, Slack, Grafana, Azure) and applies them by
writing to .env and restarting the affected docker-compose services.

Security properties (do not weaken these without good reason):
- Every request requires Authorization: Bearer <CONTROL_PLANE_TOKEN>.
- Only a fixed allowlist of env var KEYS can be written per connector —
  arbitrary keys in a request body are always rejected.
- Values are rejected if they contain newlines (prevents .env injection:
  a value with an embedded newline could otherwise inject extra
  KEY=VALUE lines into the file).
- Secret values are never returned to the frontend — GET /api/status only
  reports booleans ("is this connector configured"), never raw values.
- Writes to .env are serialized with a lock so concurrent requests can't
  corrupt the file.
"""

import os
import re
import subprocess
import threading

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()



app = Flask(__name__)

CORS(app, origins=os.environ.get("CONTROL_PLANE_CORS_ORIGINS", "http://localhost:5173").split(","))

ENV_FILE = os.environ.get("ENV_FILE_PATH", ".env")
PROJECT_DIR = os.environ.get("PROJECT_DIR", ".")
ADMIN_TOKEN = os.environ.get("CONTROL_PLANE_TOKEN")

if not ADMIN_TOKEN:
    raise SystemExit("FATAL: CONTROL_PLANE_TOKEN is not set. Refusing to start.")

# Which env var keys each connector may write, and which docker-compose
# services need restarting when they change. Nothing outside this map can
# ever be written, no matter what the request body contains.
CONNECTOR_ENV_MAP = {
    "github": {
        "keys": ["GITHUB_TOKEN"],
        "services": ["github-mcp", "watcher"],
    },
    "jira": {
        "keys": ["JIRA_URL", "JIRA_USERNAME", "JIRA_API_KEY"],
        "services": ["jira-mcp", "holmes"],
    },
    "slack": {
        "keys": ["SLACK_TOKEN", "SLACK_CHANNEL"],
        "services": ["slack-mcp", "slack-listener", "watcher", "holmes"],
    },
    "grafana": {
        "keys": ["GRAFANA_URL", "GRAFANA_TOKEN"],
        "services": ["grafana-mcp", "holmes"],
    },
    "azure": {
        "keys": ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"],
        "services": ["holmes", "watcher"],
    },
    # k8s-remediation intentionally omitted: its kubeconfig is a static
    # volume mount in docker-compose.yaml, not an env var — not configurable
    # via this API without a docker-compose.yaml change, so we don't pretend
    # it is.
}

ALL_CONNECTOR_KEYS = {key for cfg in CONNECTOR_ENV_MAP.values() for key in cfg["keys"]}

_write_lock = threading.Lock()
_UNSAFE_VALUE_RE = re.compile(r"[\r\n]")


def require_auth():
    """Returns an error Response if unauthorized, else None."""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    if not token or token != ADMIN_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def read_env_file() -> dict:
    if not os.path.exists(ENV_FILE):
        return {}
    values = {}
    with open(ENV_FILE, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key] = value
    return values


def update_env_file(updates: dict) -> None:
    with _write_lock:
        lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r") as f:
                lines = f.read().split("\n")

        seen = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            key, _, _ = stripped.partition("=")
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                new_lines.append(line)

        for key, value in updates.items():
            if key not in seen:
                new_lines.append(f"{key}={value}")

        tmp_path = ENV_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            f.write("\n".join(new_lines))
        os.replace(tmp_path, ENV_FILE)


# def restart_services(services: list[str]) -> None:
#     # --no-deps: only recreate the named services, not everything that
#     # depends on them (avoids cascading restarts of unrelated containers).
#     args = [
#     "docker",
#     "compose",
#     "up",
#     "-d",
#     "--force-recreate",
#     "--no-deps",
#     *services,
# ]
#     result = subprocess.run(
#         args, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120,
#     )
#     print(f"[control-plane] {' '.join(args)}\n{result.stdout}\n{result.stderr}")
#     if result.returncode != 0:
#         raise RuntimeError(result.stderr[:500] or "docker compose up failed")




def restart_services(services: list[str]) -> None:
    args = [
        "docker", "compose", "up", "-d",
        "--force-recreate", "--no-deps",
        *services,
    ]

    clean_env = {k: v for k, v in os.environ.items() if k not in ALL_CONNECTOR_KEYS}
    result = subprocess.run(
        args, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120,
        env=clean_env,
    )
    print(f"[control-plane] {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500] or "docker compose up failed")
    

def wait_for_healthy(services: list[str], timeout: int = 60, interval: float = 2.0) -> list[str]:
    """Poll until every service is Running (and Healthy, if it defines a
    healthcheck) or the timeout elapses. Returns the list of services that
    never became ready in time (empty list = all good)."""
    import time, json

    deadline = time.time() + timeout
    pending = set(services)

    while pending and time.time() < deadline:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json", *pending],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = info.get("Service")
                state = info.get("State", "")
                health = info.get("Health", "")
                is_ready = state == "running" and health in ("", "healthy")
                if is_ready and name in pending:
                    pending.discard(name)
        if pending:
            time.sleep(interval)

    return sorted(pending)  # anything left here never became ready in time


@app.get("/api/status")
def get_status():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    current = read_env_file()
    status = {
        connector_id: all(current.get(k, "").strip() for k in cfg["keys"])
        for connector_id, cfg in CONNECTOR_ENV_MAP.items()
    }
    return jsonify({"status": status})


@app.post("/api/credentials")
def set_credentials():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    body = request.get_json(silent=True) or {}
    connector = body.get("connector")
    values = body.get("values")

    cfg = CONNECTOR_ENV_MAP.get(connector)
    if not cfg:
        return jsonify({"error": f"Unknown or unsupported connector: {connector}"}), 400
    if not isinstance(values, dict):
        return jsonify({"error": "Missing values object"}), 400

    # Reject any key not explicitly allowed for this connector.
    for key in values:
        if key not in cfg["keys"]:
            return jsonify({"error": f"Field not allowed for {connector}: {key}"}), 400

    updates = {}
    for key in cfg["keys"]:
        if key not in values:
            return jsonify({"error": f"Missing required field: {key}"}), 400
        value = values[key]
        if not isinstance(value, str) or _UNSAFE_VALUE_RE.search(value):
            return jsonify({"error": f"Invalid value for {key} (no newlines allowed)"}), 400
        updates[key] = value

    try:
        update_env_file(updates)
    except Exception as e:
        print(f"[control-plane] Failed to write .env: {e}")
        return jsonify({"error": "Failed to write configuration"}), 500

    try:
        restart_services(cfg["services"])
    except Exception as e:
        print(f"[control-plane] Failed to restart services: {e}")
        return jsonify({
            "warning": "Saved, but restarting services failed. They may be running with stale credentials.",
            "detail": str(e),
        }), 207
    
    not_ready = wait_for_healthy(cfg["services"])
    if not_ready:
        return jsonify({
            "warning": f"Restarted, but still starting up: {', '.join(not_ready)}",
            "restarted": cfg["services"],
        }), 202

    return jsonify({"ok": True, "restarted": cfg["services"]})


if __name__ == "__main__":
    port = int(os.environ.get("CONTROL_PLANE_PORT", "4000"))
    app.run(host="0.0.0.0", port=port)