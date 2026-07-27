"""
sonarqube_checks.py
----------------------
SonarQube integration: detects new commits on the default branch, clones/
updates a local checkout, runs sonar-scanner, waits for analysis to finish,
fetches issues, and asks Holmes to fix what it safely can via a PR.
"""

import os
import subprocess
import time

import requests

from .config import (
    GITHUB_REPO, SONARQUBE_ENABLED, SONARQUBE_URL, SONARQUBE_ORG, SONARQUBE_TOKEN,
    SONARQUBE_PROJECT_KEY, SONARQUBE_SEVERITIES, SONARQUBE_MAX_ISSUES,
    GIT_CLONE_DIR, GIT_DEFAULT_BRANCH,
)
from .prompts import SONARQUBE_FIX_PROMPT_TEMPLATE
from .state import load_state, save_state, extract_json
from .slack_notify import notify_slack
from .holmes_client import ask_holmes
from .escalation import escalate_and_autofix


# ── SonarQube / git helpers ────────────────────────────────────────────────
def run_git(args, cwd=None, timeout=120):
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[watcher] git {' '.join(args[:3])}... timed out after {timeout}s")
        return None


def get_latest_commit_sha(branch=None):
    """Uses the GitHub REST API (not git) so we don't need a local clone just
    to check whether anything changed."""
    ref = branch or "HEAD"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{ref}"
    headers = {"Accept": "application/vnd.github+json"}
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["sha"], data.get("commit", {}).get("committer", {}).get("date", "")
    except Exception as e:
        print(f"[watcher] Failed to fetch latest commit SHA for {GITHUB_REPO}: {e}")
        return None, None


def get_default_branch():
    if GIT_DEFAULT_BRANCH:
        return GIT_DEFAULT_BRANCH
    url = f"https://api.github.com/repos/{GITHUB_REPO}"
    headers = {"Accept": "application/vnd.github+json"}
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["default_branch"]
    except Exception as e:
        print(f"[watcher] Failed to fetch default branch for {GITHUB_REPO}: {e}, falling back to 'main'")
        return "main"


def clone_or_update_repo(branch):
    """Clones the repo into GIT_CLONE_DIR if not present, otherwise fetches
    and resets to the latest commit on `branch`. Uses GITHUB_TOKEN for auth
    so this works on private repos too."""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        clone_url = f"https://{github_token}@github.com/{GITHUB_REPO}.git"
    else:
        clone_url = f"https://github.com/{GITHUB_REPO}.git"

    if os.path.isdir(os.path.join(GIT_CLONE_DIR, ".git")):
        result = run_git(["fetch", "origin", branch], cwd=GIT_CLONE_DIR)
        if result is None or result.returncode != 0:
            print(f"[watcher] git fetch failed: {(result.stderr[:300] if result else '')}")
            return False
        result = run_git(["reset", "--hard", f"origin/{branch}"], cwd=GIT_CLONE_DIR)
        if result is None or result.returncode != 0:
            print(f"[watcher] git reset failed: {(result.stderr[:300] if result else '')}")
            return False
        return True

    os.makedirs(os.path.dirname(GIT_CLONE_DIR), exist_ok=True)
    result = run_git(["clone", "--branch", branch, "--single-branch", clone_url, GIT_CLONE_DIR], timeout=180)
    if result is None or result.returncode != 0:
        # Don't leak the token into logs if clone fails.
        safe_err = (result.stderr or "").replace(github_token, "***") if result else ""
        print(f"[watcher] git clone failed: {safe_err[:300]}")
        return False
    return True


def run_sonar_scan():
    args = [
        "sonar-scanner",
        f"-Dsonar.projectKey={SONARQUBE_PROJECT_KEY}",
        f"-Dsonar.sources=.",
        f"-Dsonar.host.url={SONARQUBE_URL}",
        f"-Dsonar.token={SONARQUBE_TOKEN}",
    ]
    if SONARQUBE_ORG:
        args.append(f"-Dsonar.organization={SONARQUBE_ORG}")
    try:
        result = subprocess.run(args, cwd=GIT_CLONE_DIR, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("[watcher] sonar-scanner timed out after 600s")
        return False
    if result.returncode != 0:
        print(f"[watcher] sonar-scanner failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
        return False
    return True


def wait_for_ce_task(timeout_sec=180):
    """Reads .scannerwork/report-task.txt for the background-task URL the
    scanner just submitted, then polls it until SUCCESS/FAILED/CANCELED —
    so we don't query for issues before the analysis has actually been
    processed server-side."""
    report_task_path = os.path.join(GIT_CLONE_DIR, ".scannerwork", "report-task.txt")
    if not os.path.exists(report_task_path):
        print("[watcher] report-task.txt not found — cannot confirm analysis completion")
        return False

    task_url = None
    with open(report_task_path) as f:
        for line in f:
            if line.startswith("ceTaskUrl="):
                task_url = line.strip().split("=", 1)[1]
                break
    if not task_url:
        print("[watcher] No ceTaskUrl found in report-task.txt")
        return False

    auth = (SONARQUBE_TOKEN, "")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = requests.get(task_url, auth=auth, timeout=30)
            resp.raise_for_status()
            status = resp.json().get("task", {}).get("status")
        except Exception as e:
            print(f"[watcher] Failed to poll CE task status: {e}")
            return False
        if status == "SUCCESS":
            return True
        if status in ("FAILED", "CANCELED"):
            print(f"[watcher] SonarQube analysis task ended with status={status}")
            return False
        time.sleep(5)
    print("[watcher] Timed out waiting for SonarQube analysis task to complete")
    return False


def get_sonarqube_issues():
    severities = SONARQUBE_SEVERITIES
    url = f"{SONARQUBE_URL}/api/issues/search"
    params = {
        "componentKeys": SONARQUBE_PROJECT_KEY,
        "statuses": "OPEN,CONFIRMED,REOPENED",
        "severities": severities,
        "ps": min(SONARQUBE_MAX_ISSUES, 100),
    }
    if SONARQUBE_ORG:
        params["organization"] = SONARQUBE_ORG
    try:
        resp = requests.get(url, params=params, auth=(SONARQUBE_TOKEN, ""), timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[watcher] Failed to fetch SonarQube issues: {e}")
        return []

    issues = []
    for issue in data.get("issues", [])[:SONARQUBE_MAX_ISSUES]:
        component = issue.get("component", "")
        # component looks like "{projectKey}:path/to/file.py"
        file_path = component.split(":", 1)[1] if ":" in component else component
        issues.append({
            "file": file_path,
            "line": issue.get("line", "?"),
            "rule": issue.get("rule", ""),
            "severity": issue.get("severity", ""),
            "message": issue.get("message", ""),
        })
    return issues


def format_issues_for_prompt(issues):
    lines = []
    for i, issue in enumerate(issues, 1):
        lines.append(
            f"{i}. [{issue['severity']}] {issue['file']}:{issue['line']} "
            f"({issue['rule']}) — {issue['message']}"
        )
    return "\n".join(lines)



# ── Core check ──────────────────────────────────────────────────────────
def check_sonarqube():
    if not SONARQUBE_ENABLED:
        return
    if not GITHUB_REPO:
        print("[watcher] SONARQUBE_ENABLED but GITHUB_REPO is not set — skipping")
        return
    if not SONARQUBE_PROJECT_KEY or not SONARQUBE_TOKEN:
        print("[watcher] SONARQUBE_PROJECT_KEY or SONARQUBE_TOKEN not set — skipping SonarQube check")
        return

    state = load_state()
    key = f"sonarqube:{GITHUB_REPO}"

    branch = get_default_branch()
    latest_sha, commit_date = get_latest_commit_sha(branch)
    if latest_sha is None:
        return  # already logged the reason in get_latest_commit_sha

    prev = state.get(key)
    if prev and prev.get("last_scanned_sha") == latest_sha:
        print(f"[watcher] SonarQube '{GITHUB_REPO}': no new commits since last scan ({latest_sha[:8]})")
        return

    print(f"[watcher] SonarQube '{GITHUB_REPO}': new commit {latest_sha[:8]} detected, running scan")

    if not clone_or_update_repo(branch):
        print(f"[watcher] SonarQube '{GITHUB_REPO}': could not clone/update repo, skipping this cycle")
        return

    if not run_sonar_scan():
        print(f"[watcher] SonarQube '{GITHUB_REPO}': scan failed, skipping this cycle")
        return

    if not wait_for_ce_task():
        print(f"[watcher] SonarQube '{GITHUB_REPO}': could not confirm analysis completion, skipping this cycle")
        return

    issues = get_sonarqube_issues()

    # Record that we've scanned this commit regardless of outcome, so we
    # don't rescan the same commit every poll cycle.
    state[key] = state.get(key, {})
    state[key]["last_scanned_sha"] = latest_sha
    state[key]["last_scanned_at"] = int(time.time())
    save_state(state)

    if not issues:
        print(f"[watcher] SonarQube '{GITHUB_REPO}': no issues at severities {SONARQUBE_SEVERITIES} for {latest_sha[:8]}")
        return

    print(f"[watcher] SonarQube '{GITHUB_REPO}': {len(issues)} issue(s) found for {latest_sha[:8]}, escalating to Holmes")

    thread_ts = notify_slack(
        f":mag: *SonarQube found {len(issues)} issue(s)* in `{GITHUB_REPO}` "
        f"(commit `{latest_sha[:8]}`, severities: {SONARQUBE_SEVERITIES})\n"
        f"Asking Holmes to review and fix what it safely can..."
    )

    fix_prompt = SONARQUBE_FIX_PROMPT_TEMPLATE.format(
        commit_sha=latest_sha, branch=branch, repo=GITHUB_REPO,
        issues_text=format_issues_for_prompt(issues),
        timestamp=int(time.time()),
    )
    try:
        fix_raw = ask_holmes(fix_prompt)
    except Exception as e:
        notify_slack(f":x: SonarQube autofix failed: {e}", thread_ts=thread_ts)
        return

    fix_result = extract_json(fix_raw)
    if fix_result is None:
        notify_slack(
            f"Couldn't parse a clean result from the SonarQube fix step. Raw output:\n```{fix_raw[:1500]}```",
            thread_ts=thread_ts,
        )
        return

    if fix_result.get("pr_opened"):
        notify_slack(
            f":white_check_mark: Opened a PR: {fix_result.get('pr_url')}\n"
            f"Fixed {fix_result.get('issues_fixed', '?')} issue(s), "
            f"skipped {fix_result.get('issues_skipped', '?')}.\n"
            f"{fix_result.get('explanation', '')}\n"
            f"_This PR was opened automatically — please review before merging._",
            thread_ts=thread_ts,
        )
    else:
        notify_slack(
            f":information_source: No PR opened. {fix_result.get('explanation', '')}",
            thread_ts=thread_ts,
        )
