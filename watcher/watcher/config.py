"""
config.py
---------
All environment-driven configuration and shared constants for the watcher.
"""

import os
import re

# ── Config ───────────────────────────────────────────────────────────────
HOLMES_URL = os.environ.get("HOLMES_URL", "http://holmes:5050/api/chat")
HOLMES_TIMEOUT_SEC = int(os.environ.get("HOLMES_TIMEOUT_SEC", "1200"))

SLACK_TOKEN = os.environ["SLACK_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]

POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "1200"))
CHECK_WINDOW_MINUTES = int(os.environ.get("CHECK_WINDOW_MINUTES", "15"))
LOG_WINDOW_MINUTES = int(os.environ.get("LOG_WINDOW_MINUTES", "20"))
HTTP_5XX_THRESHOLD = int(os.environ.get("HTTP_5XX_THRESHOLD", "1"))

AZURE_APP_SERVICE = os.environ.get("AZURE_APP_SERVICE", "jaano-new")
AZURE_RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "appsvc_linux_centralindia")
AZURE_SUBSCRIPTION_ID = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")

# ── New config (add near the existing AZURE_* config block) ──────────────
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
K8S_DEPLOYMENT = os.environ.get("K8S_DEPLOYMENT", "")  # blank = watch all pods in namespace
K8S_RESTART_THRESHOLD = int(os.environ.get("K8S_RESTART_THRESHOLD", "3"))
K8S_LOG_WINDOW_MINUTES = int(os.environ.get("K8S_LOG_WINDOW_MINUTES", "20"))
K8S_ENABLED = os.environ.get("K8S_ENABLED", "true").lower() == "true"


GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "myorg/myrepo" — required for auto-fix step

STATE_FILE = os.environ.get("STATE_FILE", "/data/watcher_state.json")

LOG_ERROR_PATTERN = re.compile(
    r"traceback|unhandled exception|exception:|critical|internal server error|\b5\d\d\b",
    re.IGNORECASE,
)

# ── New config (add near the K8S_* config block) ──────────────────────────
SONARQUBE_ENABLED = os.environ.get("SONARQUBE_ENABLED", "true").lower() == "true"
SONARQUBE_URL = os.environ.get("SONARQUBE_URL", "http://sonarqube:9000")
SONARQUBE_TOKEN = os.environ.get("SONARQUBE_TOKEN", "")
SONARQUBE_PROJECT_KEY = os.environ.get("SONARQUBE_PROJECT_KEY", "")
SONARQUBE_SEVERITIES = os.environ.get("SONARQUBE_SEVERITIES", "BLOCKER,CRITICAL")
SONARQUBE_MAX_ISSUES = int(os.environ.get("SONARQUBE_MAX_ISSUES", "20"))
GIT_CLONE_DIR = os.environ.get("GIT_CLONE_DIR", "/data/repo-clone")
GIT_DEFAULT_BRANCH = os.environ.get("GIT_DEFAULT_BRANCH", "")  # blank = auto-detect via GitHub API
