"""
escalation.py
--------------
Shared "escalate -> investigate -> fix" pipeline used by both the Azure
and Kubernetes checks once their cheap (non-LLM) check has flagged a
possible issue and Holmes has confirmed it's real.
"""

import time

from .config import GITHUB_REPO
from .prompts import INVESTIGATE_PROMPT_TEMPLATE, EXECUTE_FIX_PROMPT_TEMPLATE
from .state import issue_signature, save_state, extract_json
from .slack_notify import notify_slack
from .holmes_client import ask_holmes


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
