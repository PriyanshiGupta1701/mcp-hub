"""
main.py
-------
Entrypoint. Polls Holmes on a schedule to check the health of Azure
resources, a Kubernetes deployment, and (via SonarQube) code quality —
alerting on Slack and opening autofix PRs for new issues.

State (which issues have already been alerted on) is persisted to a JSON
file so restarts / repeated polls don't spam Slack with duplicate alerts
for the same ongoing issue.

Run with: python main.py
"""

import sys
import time

from watcher.config import (
    AZURE_APP_SERVICE, K8S_NAMESPACE, K8S_DEPLOYMENT, GITHUB_REPO,
    POLL_INTERVAL_SEC,
)
from watcher.azure_checks import az_login, check_azure
from watcher.kubernetes_checks import check_kubernetes
from watcher.sonarqube_checks import check_sonarqube


# ── Updated main() — add the SonarQube check to the cycle ────────────────
def main():
    print(f"[watcher] Starting. Polling Azure App Service '{AZURE_APP_SERVICE}', "
          f"Kubernetes '{K8S_NAMESPACE}/{K8S_DEPLOYMENT or 'all'}', "
          f"and SonarQube for '{GITHUB_REPO}' every {POLL_INTERVAL_SEC}s")
    az_login()
    while True:
        try:
            check_azure()
        except Exception as e:
            print(f"[watcher] Error during Azure check cycle: {e}", file=sys.stderr)
        try:
            check_kubernetes()
        except Exception as e:
            print(f"[watcher] Error during Kubernetes check cycle: {e}", file=sys.stderr)
        try:
            check_sonarqube()
        except Exception as e:
            print(f"[watcher] Error during SonarQube check cycle: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
