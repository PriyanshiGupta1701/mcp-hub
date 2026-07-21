"""
watcher.py
----------
Polls Holmes on a schedule to check the health of Azure resources.
On a NEW issue (not a repeat of one already alerted on):
  1. Posts a brief Slack notification.
  2. Asks Holmes to investigate root cause, patch the code, and open a GitHub PR.
  3. Posts a follow-up Slack message with the outcome (PR link or "no code fix found").

State (which issues have already been alerted on) is persisted to a JSON file
so restarts / repeated polls don't spam Slack with duplicate alerts for the
same ongoing issue.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests
from slack_sdk import WebClient

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

slack = WebClient(token=SLACK_TOKEN)

# ── Prompts ──────────────────────────────────────────────────────────────
SUMMARIZE_PROMPT_TEMPLATE = """A direct health check (no LLM involved) on Azure App Service '{app}' in
resource group '{rg}' just found signs of a possible problem. This evidence
was already gathered directly — you do not need to re-fetch it, though you
may use tools briefly for additional context if genuinely necessary:

App state: {app_state}
HTTP 5xx count in the last {window} minutes: {http_5xx_count}
Error lines found in application logs:
{log_excerpts}

The Azure subscription ID is '{subscription_id}' if you need it for any tool calls.

Based on this evidence, respond with ONLY a JSON object, no markdown, no extra
commentary, in exactly this shape:
{{"issue_found": true or false, "severity": "low" | "medium" | "high" | "critical", "category": "short-machine-readable-slug", "summary": "one sentence, under 200 characters", "details": "2-4 sentences describing the issue and root cause if evident"}}

If, on reflection, this evidence doesn't actually indicate a real problem
(e.g. a transient blip, expected behavior), return issue_found: false and
explain why in the summary."""

INVESTIGATE_PROMPT_TEMPLATE = """You previously found this issue with Azure App Service '{app}':
Category: {category}
Summary: {summary}
Details: {details}

Investigate the root cause by examining the code in the GitHub repository '{repo}'
(use your GitHub tools to browse and search the repo — look for the code path
that would produce this behavior).

Respond with ONLY a JSON object, no markdown, no extra commentary, in exactly this shape:
{{"fixable": true or false, "explanation": "1-3 sentences on the root cause, or why it's not fixable in code (e.g. pure infra/quota/config issue)", "proposed_change": "a concise, specific description of the exact code/config change to make — empty string if not fixable"}}"""

EXECUTE_FIX_PROMPT_TEMPLATE = """In the GitHub repository '{repo}', make this specific change:

{proposed_change}

(Context: this fixes — {explanation})

1. Create a new branch named 'holmes-autofix/{category}-{timestamp}' from the default branch.
2. Make the change as commit(s) on that branch.
3. Open a pull request from that branch into the default branch. Title it clearly
   referencing the issue. In the PR description include what was observed, the
   root cause, and exactly what was changed. State explicitly that this PR was
   opened automatically by Holmes and should be reviewed by a human before merging.
   Do NOT merge the PR yourself.

Respond with ONLY a JSON object, no markdown, no extra commentary, in exactly this shape:
{{"pr_opened": true or false, "pr_url": "url or empty string", "explanation": "1-3 sentences"}}"""


# ── New prompt template (add alongside SUMMARIZE_PROMPT_TEMPLATE) ────────
SUMMARIZE_PROMPT_TEMPLATE_K8S = """A direct health check (no LLM involved) on the Kubernetes deployment '{deployment}'
(namespace '{namespace}') just found signs of a possible problem. This evidence
was already gathered directly — you do not need to re-fetch it, though you
may use tools briefly for additional context if genuinely necessary:

Pod status summary:
{pod_summary}

Recent Warning events:
{events_summary}

Error lines / tracebacks found in pod logs:
{log_excerpts}

Based on this evidence, respond with ONLY a JSON object, no markdown, no extra
commentary, in exactly this shape:
{{"issue_found": true or false, "severity": "low" | "medium" | "high" | "critical", "category": "short-machine-readable-slug", "summary": "one sentence, under 200 characters", "details": "2-4 sentences describing the issue and root cause if evident"}}

If, on reflection, this evidence doesn't actually indicate a real problem
(e.g. a transient blip, expected behavior, deploy-in-progress), return
issue_found: false and explain why in the summary."""


# ── Refactor: extract the shared "escalate -> investigate -> fix" flow ──
# This is the second half of the old check_azure(), generalized so both
# check_azure() and check_kubernetes() call the same pipeline instead of
# duplicating it.
def escalate_and_autofix(key, state, category, summary, details, severity, resource_label):
    """Handles: dedupe against known issue, Slack alert, investigate, and
    (if fixable) execute the fix as a PR. Mutates and saves `state`."""
    sig = issue_signature(category, summary)

    prev = state.get(key)
    if prev and prev.get("signature") == sig:
        print(f"[watcher] {resource_label}: known issue still active ({sig}), not re-alerting")
        return

    thread_ts = notify_slack(
        f":rotating_light: *{resource_label} issue detected*\n"
        f"*Severity:* {severity}\n"
        f"*Summary:* {summary}\n"
        f"*Details:* {details}\n"
        f"Investigating for a code-level fix now..."
    )

    state[key] = {
        "signature": sig,
        "summary": summary,
        "severity": severity,
        "status": "alerted",
        "thread_ts": thread_ts,
        "detected_at": int(time.time()),
    }
    save_state(state)

    if not GITHUB_REPO:
        notify_slack("`GITHUB_REPO` isn't configured, so I'm skipping the auto-fix step.", thread_ts=thread_ts)
        return

    investigate_prompt = INVESTIGATE_PROMPT_TEMPLATE.format(
        app=resource_label, category=category, summary=summary, details=details, repo=GITHUB_REPO,
    )
    try:
        investigate_raw = ask_holmes(investigate_prompt)
    except Exception as e:
        notify_slack(
            f":x: The investigation step failed and couldn't complete: {e}\n"
            f"No PR was opened — this will need a manual look.",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "investigate_failed"
        save_state(state)
        return

    investigate_result = extract_json(investigate_raw)
    if investigate_result is None:
        notify_slack(
            f"Investigation finished but I couldn't parse a clean result. Raw output:\n```{investigate_raw[:1500]}```",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "investigate_parse_failed"
        save_state(state)
        return

    if not investigate_result.get("fixable"):
        notify_slack(
            f":information_source: No auto-fix PR opened. {investigate_result.get('explanation', '')}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "no_fix"
        save_state(state)
        return

    proposed_change = investigate_result.get("proposed_change", "")
    notify_slack(
        f":mag: Found a fixable root cause, opening a PR now...\n_{investigate_result.get('explanation', '')}_",
        thread_ts=thread_ts,
    )

    execute_prompt = EXECUTE_FIX_PROMPT_TEMPLATE.format(
        repo=GITHUB_REPO, proposed_change=proposed_change,
        explanation=investigate_result.get("explanation", ""),
        category=category, timestamp=int(time.time()),
    )
    try:
        fix_raw = ask_holmes(execute_prompt)
    except Exception as e:
        notify_slack(
            f":x: Found a fix but opening the PR failed: {e}\n"
            f"Proposed change (not applied): {proposed_change}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "fix_failed"
        save_state(state)
        return

    fix_result = extract_json(fix_raw)
    if fix_result is None:
        notify_slack(
            f"PR step finished but I couldn't parse a clean result. Raw output:\n```{fix_raw[:1500]}```",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "fix_parse_failed"
        save_state(state)
        return

    if fix_result.get("pr_opened"):
        notify_slack(
            f":white_check_mark: Opened a PR: {fix_result.get('pr_url')}\n{fix_result.get('explanation', '')}\n"
            f"_This PR was opened automatically — please review before merging._",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "pr_opened"
        state[key]["pr_url"] = fix_result.get("pr_url")
    else:
        notify_slack(
            f":information_source: No auto-fix PR opened. {fix_result.get('explanation', '')}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "no_fix"
    save_state(state)


# ── Kubernetes helpers ────────────────────────────────────────────────────
def run_kubectl(args, timeout=60):
    try:
        return subprocess.run(["kubectl"] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[watcher] kubectl {' '.join(args[:3])}... timed out after {timeout}s")
        return None
    except FileNotFoundError:
        print("[watcher] kubectl not found — is it installed in the watcher image?")
        return None


def get_pod_status():
    """Returns (issue_found, pod_summary_text, target_pod_names).
    target_pod_names is used to scope log fetching to only the pods that
    matched K8S_DEPLOYMENT (or all pods in the namespace if unset)."""
    args = ["get", "pods", "-n", K8S_NAMESPACE, "-o", "json"]
    result = run_kubectl(args, timeout=30)
    if result is None or result.returncode != 0:
        print(f"[watcher] kubectl get pods failed: {(result.stderr[:300] if result else '')}")
        return False, "(could not fetch pod status)", []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[watcher] Failed to parse kubectl get pods output: {e}")
        return False, "(could not parse pod status)", []

    issue_found = False
    lines = []
    target_pods = []

    for pod in data.get("items", []):
        name = pod.get("metadata", {}).get("name", "unknown")
        if K8S_DEPLOYMENT and K8S_DEPLOYMENT not in name:
            continue
        target_pods.append(name)

        phase = pod.get("status", {}).get("phase", "Unknown")
        container_statuses = pod.get("status", {}).get("containerStatuses", []) or []
        max_restarts = max((cs.get("restartCount", 0) for cs in container_statuses), default=0)

        bad_states = []
        for cs in container_statuses:
            state = cs.get("state", {})
            if "waiting" in state:
                reason = state["waiting"].get("reason", "")
                if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError"):
                    bad_states.append(reason)
            if "terminated" in state:
                reason = state["terminated"].get("reason", "")
                if reason not in ("Completed",):
                    bad_states.append(f"terminated:{reason}")

        is_problem = (
            phase not in ("Running", "Succeeded")
            or max_restarts >= K8S_RESTART_THRESHOLD
            or bool(bad_states)
        )
        if is_problem:
            issue_found = True

        lines.append(
            f"- {name}: phase={phase}, restarts={max_restarts}"
            + (f", issues={','.join(bad_states)}" if bad_states else "")
        )

    if not lines:
        return False, f"(no pods matched deployment filter '{K8S_DEPLOYMENT}' in namespace '{K8S_NAMESPACE}')", []

    return issue_found, "\n".join(lines), target_pods


def get_recent_warning_events():
    result = run_kubectl(
        ["get", "events", "-n", K8S_NAMESPACE, "--field-selector", "type=Warning",
         "--sort-by=.lastTimestamp", "-o",
         "custom-columns=TIME:.lastTimestamp,OBJECT:.involvedObject.name,REASON:.reason,MESSAGE:.message"],
        timeout=30,
    )
    if result is None or result.returncode != 0:
        return "(could not fetch events)"
    lines = result.stdout.strip().splitlines()
    if len(lines) <= 1:  # just the header, or nothing
        return "(no recent warning events)"
    return "\n".join(lines[-15:])  # header + last 15 events


def get_pod_log_excerpts(pod_names, max_excerpts=15):
    """Fetches recent logs for the given pods and extracts error lines /
    full traceback blocks, reusing the same block-extraction logic as the
    Azure log parser (it's plain text processing, not Azure-specific)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=K8S_LOG_WINDOW_MINUTES)
    traceback_blocks = {}
    single_line_excerpts = []
    skipped_undated = 0

    for pod_name in pod_names:
        # --previous also, in case the container already restarted/crashed —
        # that's often where the actual exception lives, not in the fresh log.
        for extra_args in ([], ["--previous"]):
            result = run_kubectl(
                ["logs", pod_name, "-n", K8S_NAMESPACE, "--timestamps",
                 "--tail=1000", *extra_args],
                timeout=30,
            )
            if result is None or result.returncode != 0:
                continue

            tail = result.stdout.splitlines()
            i = 0
            while i < len(tail):
                line = tail[i]
                is_traceback_start = TRACEBACK_START_PATTERN.search(line)
                if not (is_traceback_start or LOG_ERROR_PATTERN.search(line)):
                    i += 1
                    continue

                line_ts = parse_log_line_timestamp(line)
                if line_ts is None:
                    skipped_undated += 1
                    i += 1
                    continue
                if line_ts < cutoff:
                    i += 1
                    continue

                if is_traceback_start:
                    block = [line.strip()]
                    j = i + 1
                    while j < len(tail) and (j - i) <= MAX_TRACEBACK_BLOCK_LINES:
                        next_stripped = tail[j].strip()
                        if not next_stripped:
                            break
                        block.append(next_stripped)
                        j += 1
                        if TRACEBACK_END_PATTERN.match(next_stripped):
                            break
                    block_text = "\n".join(block)
                    sig = (block[0], block[-1])
                    if sig in traceback_blocks:
                        traceback_blocks[sig]["count"] += 1
                    else:
                        traceback_blocks[sig] = {"text": block_text, "count": 1}
                    i = j
                else:
                    single_line_excerpts.append(line.strip())
                    i += 1

    if skipped_undated:
        print(f"[watcher] Skipped {skipped_undated} pod log line(s) with no parseable timestamp")

    excerpts = []
    for info in traceback_blocks.values():
        text = info["text"]
        if info["count"] > 1:
            text += f"\n(this exact traceback occurred {info['count']} times in the last {K8S_LOG_WINDOW_MINUTES} minutes)"
        excerpts.append(text)
    excerpts.extend(dict.fromkeys(single_line_excerpts))

    return excerpts[-max_excerpts:]


def cheap_check_kubernetes():
    """Non-LLM health check for the Kubernetes cluster. Mirrors
    cheap_check_azure()'s shape: (issue_found, evidence_dict)."""
    pod_issue, pod_summary, target_pods = get_pod_status()
    events_summary = get_recent_warning_events()
    log_excerpts = get_pod_log_excerpts(target_pods) if target_pods else []

    issue_found = pod_issue or bool(log_excerpts)
    return issue_found, {
        "pod_summary": pod_summary,
        "events_summary": events_summary,
        "log_excerpts": log_excerpts,
    }


def check_kubernetes():
    if not K8S_ENABLED:
        return
    state = load_state()
    key = f"k8s:{K8S_NAMESPACE}:{K8S_DEPLOYMENT or 'all'}"

    issue_found_cheap, evidence = cheap_check_kubernetes()

    if not issue_found_cheap:
        if key in state:
            print(f"[watcher] Kubernetes '{K8S_NAMESPACE}/{K8S_DEPLOYMENT or 'all'}': issue appears resolved, clearing state")
            del state[key]
            save_state(state)
        else:
            print(f"[watcher] Kubernetes '{K8S_NAMESPACE}/{K8S_DEPLOYMENT or 'all'}': healthy (no LLM call made)")
        return

    print(f"[watcher] Kubernetes '{K8S_NAMESPACE}/{K8S_DEPLOYMENT or 'all'}': cheap check found a possible issue, escalating to Holmes")
    log_excerpts_text = "\n---\n".join(evidence["log_excerpts"]) or "(none found)"
    prompt = SUMMARIZE_PROMPT_TEMPLATE_K8S.format(
        deployment=K8S_DEPLOYMENT or "(all pods)", namespace=K8S_NAMESPACE,
        pod_summary=evidence["pod_summary"], events_summary=evidence["events_summary"],
        log_excerpts=log_excerpts_text,
    )
    raw = ask_holmes(prompt)
    result = extract_json(raw)

    if result is None:
        print(f"[watcher] Could not parse k8s summarize response, skipping this cycle: {raw[:300]}")
        return

    if not result.get("issue_found"):
        if key in state:
            print(f"[watcher] Kubernetes '{K8S_NAMESPACE}': issue appears resolved, clearing state")
            del state[key]
            save_state(state)
        else:
            print(f"[watcher] Kubernetes '{K8S_NAMESPACE}': healthy")
        return

    resource_label = f"Kubernetes deployment '{K8S_DEPLOYMENT or 'all pods'}' (namespace {K8S_NAMESPACE})"
    escalate_and_autofix(
        key=key, state=state,
        category=result.get("category", "issue"),
        summary=result.get("summary", "Unspecified issue"),
        details=result.get("details", ""),
        severity=result.get("severity", "unknown"),
        resource_label=resource_label,
    )


# ── Helpers ──────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_FILE)


def extract_json(text):
    """Pull the first {...} JSON object out of Holmes's reply, tolerating
    markdown code fences or stray prose around it."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ask_holmes(question, model=None, max_attempts=6, retry_wait_sec=60):
    """POST to Holmes, retrying up to max_attempts times (retry_wait_sec apart)
    on the known Gemini 'analysis: None' 500 error (model finished tool calls
    but produced no summary), on 429s, and on read timeouts. Every retry
    re-runs the full investigation from scratch — Holmes's API is stateless,
    there's no way to resume a partial tool-call chain — so this can get
    expensive: worst case is max_attempts * (HOLMES_TIMEOUT_SEC + retry_wait_sec).
    Pass model= to override Holmes's default (e.g. for prompts too complex
    for the lite model to reliably produce any output for at all)."""
    payload = {"ask": question}
    if model:
        payload["model"] = model
    last_error = None
    for attempt in range(max_attempts):
        try:
            resp = requests.post(HOLMES_URL, json=payload, timeout=HOLMES_TIMEOUT_SEC)
        except requests.exceptions.ReadTimeout:
            last_error = f"Holmes timed out after {HOLMES_TIMEOUT_SEC}s"
            print(f"[watcher] {last_error} (attempt {attempt + 1}/{max_attempts})")
            if attempt < max_attempts - 1:
                time.sleep(retry_wait_sec)
                continue
            break

        if resp.status_code == 429:
            last_error = "429 rate limited"
            print(f"[watcher] Rate limited by Holmes (attempt {attempt + 1}/{max_attempts}), waiting {retry_wait_sec}s")
            if attempt < max_attempts - 1:
                time.sleep(retry_wait_sec)
                continue
            break

        if resp.status_code == 500:
            body_text = resp.text or ""
            if "analysis" in body_text and ("NoneType" in body_text or "None" in body_text):
                last_error = "Holmes repeatedly returned no summary (analysis: None)"
                print(f"[watcher] Holmes returned no summary (attempt {attempt + 1}/{max_attempts}), retrying in {retry_wait_sec}s")
                if attempt < max_attempts - 1:
                    time.sleep(retry_wait_sec)
                    continue
                break
            # Some other 500 — not the known retryable case, fail fast rather
            # than burn 6 retries on a bug retrying can't fix.
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        return data.get("analysis") or ""

    raise RuntimeError(last_error or f"ask_holmes failed after {max_attempts} attempts")


def issue_signature(category, summary):
    return hashlib.sha256(f"{category}:{summary}".encode()).hexdigest()[:16]


def notify_slack(text, thread_ts=None):
    resp = slack.chat_postMessage(channel=SLACK_CHANNEL, text=text, thread_ts=thread_ts)
    return resp["ts"]


# ── Cheap (non-LLM) Azure check ─────────────────────────────────────────
def az_login():
    if not (AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID):
        print("[watcher] Azure service principal env vars not fully set — cheap check will be skipped")
        return False
    try:
        subprocess.run(
            ["az", "login", "--service-principal",
             "-u", AZURE_CLIENT_ID, "-p", AZURE_CLIENT_SECRET, "--tenant", AZURE_TENANT_ID],
            check=True, capture_output=True, text=True, timeout=60,
        )
        if AZURE_SUBSCRIPTION_ID:
            subprocess.run(
                ["az", "account", "set", "--subscription", AZURE_SUBSCRIPTION_ID],
                check=True, capture_output=True, text=True, timeout=30,
            )
        print("[watcher] Azure CLI login successful")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[watcher] Azure CLI login failed: {e.stderr[:300] if e.stderr else e}", file=sys.stderr)
        return False


def run_az(args, timeout=60):
    try:
        return subprocess.run(["az"] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[watcher] az {' '.join(args[:3])}... timed out after {timeout}s")
        return None


def get_app_state():
    result = run_az([
        "webapp", "show", "--name", AZURE_APP_SERVICE,
        "--resource-group", AZURE_RESOURCE_GROUP, "--query", "state", "-o", "tsv",
    ], timeout=30)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def get_http_5xx_count():
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=CHECK_WINDOW_MINUTES)
    resource_id = (
        f"/subscriptions/{AZURE_SUBSCRIPTION_ID}/resourceGroups/{AZURE_RESOURCE_GROUP}"
        f"/providers/Microsoft.Web/sites/{AZURE_APP_SERVICE}"
    )
    result = run_az([
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", "Http5xx",
        "--interval", "PT1M",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "-o", "json",
    ], timeout=60)
    if result is None or result.returncode != 0:
        print(f"[watcher] az monitor metrics list failed: {(result.stderr[:300] if result else '')}")
        return None
    try:
        data = json.loads(result.stdout)
        total = 0
        for series in data.get("value", []):
            for ts in series.get("timeseries", []):
                for point in ts.get("data", []):
                    total += point.get("total") or 0
        return int(total)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[watcher] Failed to parse metrics response: {e}")
        return None


# Matches leading ISO8601 timestamps like '2026-07-10T10:31:18.8333172Z' or
# '2026-07-10T08:05:14.5912469+00:00' that Azure prefixes onto log lines.
LOG_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})?")


def parse_log_line_timestamp(line):
    match = LOG_TIMESTAMP_PATTERN.match(line)
    if not match:
        return None
    ts_str = match.group(1)
    tz_str = match.group(2) or "+00:00"
    if tz_str == "Z":
        tz_str = "+00:00"
    try:
        # Truncate sub-second precision to microseconds (fromisoformat's limit)
        if "." in ts_str:
            head, frac = ts_str.split(".")
            ts_str = f"{head}.{frac[:6]}"
        return datetime.fromisoformat(ts_str + tz_str)
    except ValueError:
        return None


TRACEBACK_START_PATTERN = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)
# The line that actually ends a Python traceback: 'SomeError: message' or
# 'module.SomeException: message', with no leading whitespace.
TRACEBACK_END_PATTERN = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")
MAX_TRACEBACK_BLOCK_LINES = 200  # safety cap only, in case no end line is found


def get_log_error_excerpts(max_excerpts=15):
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "logs.zip")
        result = run_az([
            "webapp", "log", "download", "--name", AZURE_APP_SERVICE,
            "--resource-group", AZURE_RESOURCE_GROUP, "--log-file", zip_path,
        ], timeout=60)
        if result is None or result.returncode != 0 or not os.path.exists(zip_path):
            print(f"[watcher] az webapp log download failed: {(result.stderr[:300] if result else '')}")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOG_WINDOW_MINUTES)
        traceback_blocks = {}  # signature -> {"text": str, "count": int}
        single_line_excerpts = []
        skipped_undated = 0

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if not name.endswith((".log", ".txt")):
                        continue
                    try:
                        with zf.open(name) as f:
                            lines = f.read().decode("utf-8", errors="ignore").splitlines()
                    except Exception:
                        continue

                    tail = lines[-2000:]  # widened since real tracebacks run long
                    i = 0
                    while i < len(tail):
                        line = tail[i]
                        is_traceback_start = TRACEBACK_START_PATTERN.search(line)
                        if not (is_traceback_start or LOG_ERROR_PATTERN.search(line)):
                            i += 1
                            continue

                        line_ts = parse_log_line_timestamp(line)
                        if line_ts is None:
                            skipped_undated += 1
                            i += 1
                            continue
                        if line_ts < cutoff:
                            i += 1
                            continue

                        if is_traceback_start:
                            block = [line.strip()]
                            j = i + 1
                            while j < len(tail) and (j - i) <= MAX_TRACEBACK_BLOCK_LINES:
                                next_stripped = tail[j].strip()
                                if not next_stripped:
                                    break
                                block.append(next_stripped)
                                j += 1
                                if TRACEBACK_END_PATTERN.match(next_stripped):
                                    break  # found the real exception line — stop here
                            block_text = "\n".join(block)
                            sig = (block[0], block[-1])  # header + exception line
                            if sig in traceback_blocks:
                                traceback_blocks[sig]["count"] += 1
                            else:
                                traceback_blocks[sig] = {"text": block_text, "count": 1}
                            i = j
                        else:
                            single_line_excerpts.append(line.strip())
                            i += 1
        except zipfile.BadZipFile:
            print("[watcher] Downloaded log file is not a valid zip")
            return []

        if skipped_undated:
            print(f"[watcher] Skipped {skipped_undated} error line(s) with no parseable timestamp")

        excerpts = []
        for info in traceback_blocks.values():
            text = info["text"]
            if info["count"] > 1:
                text += f"\n(this exact traceback occurred {info['count']} times in the last {LOG_WINDOW_MINUTES} minutes)"
            excerpts.append(text)
        excerpts.extend(dict.fromkeys(single_line_excerpts))  # dedupe, keep order

        return excerpts[-max_excerpts:]


def cheap_check_azure():
    """Non-LLM health check: pulls real Azure data directly via the CLI.
    Returns (issue_found, evidence_dict). Costs zero LLM calls."""
    evidence = {
        "app_state": get_app_state(),
        "http_5xx_count": get_http_5xx_count(),
        "log_error_excerpts": get_log_error_excerpts(),
    }
    issue_found = (
        (evidence["app_state"] is not None and evidence["app_state"].lower() != "running")
        or (evidence["http_5xx_count"] is not None and evidence["http_5xx_count"] >= HTTP_5XX_THRESHOLD)
        or bool(evidence["log_error_excerpts"])
    )
    return issue_found, evidence


# ── Core check ───────────────────────────────────────────────────────────
def check_azure():
    state = load_state()
    key = f"azure:{AZURE_APP_SERVICE}"

    issue_found_cheap, evidence = cheap_check_azure()

    if not issue_found_cheap:
        if key in state:
            print(f"[watcher] Azure '{AZURE_APP_SERVICE}': issue appears resolved, clearing state")
            del state[key]
            save_state(state)
        else:
            print(f"[watcher] Azure '{AZURE_APP_SERVICE}': healthy (no LLM call made)")
        return

    print(f"[watcher] Azure '{AZURE_APP_SERVICE}': cheap check found a possible issue, escalating to Holmes")
    log_excerpts_text = "\n---\n".join(evidence["log_error_excerpts"]) or "(none found)"
    prompt = SUMMARIZE_PROMPT_TEMPLATE.format(
        app=AZURE_APP_SERVICE, rg=AZURE_RESOURCE_GROUP, window=CHECK_WINDOW_MINUTES,
        app_state=evidence["app_state"] or "unknown",
        http_5xx_count=evidence["http_5xx_count"] if evidence["http_5xx_count"] is not None else "unknown",
        log_excerpts=log_excerpts_text,
        subscription_id=AZURE_SUBSCRIPTION_ID or "not provided",
    )
    raw = ask_holmes(prompt)
    result = extract_json(raw)

    if result is None:
        print(f"[watcher] Could not parse summarize response, skipping this cycle: {raw[:300]}")
        return

    if not result.get("issue_found"):
        if key in state:
            print(f"[watcher] Azure '{AZURE_APP_SERVICE}': issue appears resolved, clearing state")
            del state[key]
            save_state(state)
        else:
            print(f"[watcher] Azure '{AZURE_APP_SERVICE}': healthy")
        return

    summary = result.get("summary", "Unspecified issue")
    severity = result.get("severity", "unknown")
    details = result.get("details", "")
    category = result.get("category", "issue")
    sig = issue_signature(category, summary)

    prev = state.get(key)
    if prev and prev.get("signature") == sig:
        print(f"[watcher] Azure '{AZURE_APP_SERVICE}': known issue still active ({sig}), not re-alerting")
        return

    # New (or changed) issue — alert.
    thread_ts = notify_slack(
        f":rotating_light: *Azure App Service issue detected* (`{AZURE_APP_SERVICE}`)\n"
        f"*Severity:* {severity}\n"
        f"*Summary:* {summary}\n"
        f"*Details:* {details}\n"
        f"Investigating for a code-level fix now..."
    )

    state[key] = {
        "signature": sig,
        "summary": summary,
        "severity": severity,
        "status": "alerted",
        "thread_ts": thread_ts,
        "detected_at": int(time.time()),
    }
    save_state(state)

    if not GITHUB_REPO:
        notify_slack("`GITHUB_REPO` isn't configured, so I'm skipping the auto-fix step.", thread_ts=thread_ts)
        return

    # Step 1: investigate only — lighter ask, no branch/commit/PR yet.
    investigate_prompt = INVESTIGATE_PROMPT_TEMPLATE.format(
        app=AZURE_APP_SERVICE,
        category=category,
        summary=summary,
        details=details,
        repo=GITHUB_REPO,
    )
    try:
        investigate_raw = ask_holmes(investigate_prompt)
    except Exception as e:
        notify_slack(
            f":x: The investigation step failed and couldn't complete: {e}\n"
            f"No PR was opened — this will need a manual look.",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "investigate_failed"
        save_state(state)
        return

    investigate_result = extract_json(investigate_raw)

    if investigate_result is None:
        notify_slack(
            f"Investigation finished but I couldn't parse a clean result. Raw output:\n```{investigate_raw[:1500]}```",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "investigate_parse_failed"
        save_state(state)
        return

    if not investigate_result.get("fixable"):
        notify_slack(
            f":information_source: No auto-fix PR opened. {investigate_result.get('explanation', '')}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "no_fix"
        save_state(state)
        return

    proposed_change = investigate_result.get("proposed_change", "")
    notify_slack(
        f":mag: Found a fixable root cause, opening a PR now...\n_{investigate_result.get('explanation', '')}_",
        thread_ts=thread_ts,
    )

    # Step 2: execute the already-decided fix — branch, commit, PR.
    execute_prompt = EXECUTE_FIX_PROMPT_TEMPLATE.format(
        repo=GITHUB_REPO,
        proposed_change=proposed_change,
        explanation=investigate_result.get("explanation", ""),
        category=category,
        timestamp=int(time.time()),
    )
    try:
        fix_raw = ask_holmes(execute_prompt)
    except Exception as e:
        notify_slack(
            f":x: Found a fix but opening the PR failed: {e}\n"
            f"Proposed change (not applied): {proposed_change}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "fix_failed"
        save_state(state)
        return

    fix_result = extract_json(fix_raw)

    if fix_result is None:
        notify_slack(
            f"PR step finished but I couldn't parse a clean result. Raw output:\n```{fix_raw[:1500]}```",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "fix_parse_failed"
        save_state(state)
        return

    if fix_result.get("pr_opened"):
        notify_slack(
            f":white_check_mark: Opened a PR: {fix_result.get('pr_url')}\n{fix_result.get('explanation', '')}\n"
            f"_This PR was opened automatically — please review before merging._",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "pr_opened"
        state[key]["pr_url"] = fix_result.get("pr_url")
    else:
        notify_slack(
            f":information_source: No auto-fix PR opened. {fix_result.get('explanation', '')}",
            thread_ts=thread_ts,
        )
        state[key]["status"] = "no_fix"

    save_state(state)


# ── Updated main() — runs both checks each cycle ──────────────────────────
def main():
    print(f"[watcher] Starting. Polling Azure App Service '{AZURE_APP_SERVICE}' "
          f"and Kubernetes '{K8S_NAMESPACE}/{K8S_DEPLOYMENT or 'all'}' every {POLL_INTERVAL_SEC}s")
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
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
