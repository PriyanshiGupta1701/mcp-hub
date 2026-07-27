"""
azure_checks.py
-----------------
Azure App Service health check: az CLI login, cheap (non-LLM) state/metric/
log inspection, escalating to Holmes and (via its own inline pipeline)
investigating + opening an autofix PR.

Note: unlike kubernetes_checks.check_kubernetes(), this module's
check_azure() does NOT call escalation.escalate_and_autofix() — it has its
own inline copy of the same escalate -> investigate -> fix steps (this
mirrors the original file exactly). Worth unifying later so Azure and K8s
share one code path.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

from .config import (
    AZURE_APP_SERVICE, AZURE_RESOURCE_GROUP, AZURE_SUBSCRIPTION_ID,
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    CHECK_WINDOW_MINUTES, LOG_WINDOW_MINUTES, HTTP_5XX_THRESHOLD,
    GITHUB_REPO, LOG_ERROR_PATTERN,
)
from .prompts import (
    SUMMARIZE_PROMPT_TEMPLATE, INVESTIGATE_PROMPT_TEMPLATE,
    EXECUTE_FIX_PROMPT_TEMPLATE,
)
from .state import load_state, save_state, extract_json, issue_signature
from .slack_notify import notify_slack
from .holmes_client import ask_holmes
from .log_parsing import (
    parse_log_line_timestamp, TRACEBACK_START_PATTERN,
    TRACEBACK_END_PATTERN, MAX_TRACEBACK_BLOCK_LINES,
)


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
