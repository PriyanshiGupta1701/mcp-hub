"""
kubernetes_checks.py
----------------------
Kubernetes health check: cheap (non-LLM) pod/event/log inspection via
kubectl, escalating to Holmes (and the shared autofix pipeline) when a
possible issue is found.
"""

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .config import (
    K8S_NAMESPACE, K8S_DEPLOYMENT, K8S_RESTART_THRESHOLD,
    K8S_LOG_WINDOW_MINUTES, K8S_ENABLED, LOG_ERROR_PATTERN,
)
from .prompts import SUMMARIZE_PROMPT_TEMPLATE_K8S
from .state import load_state, save_state, extract_json
from .holmes_client import ask_holmes
from .escalation import escalate_and_autofix
from .log_parsing import (
    parse_log_line_timestamp, TRACEBACK_START_PATTERN,
    TRACEBACK_END_PATTERN, MAX_TRACEBACK_BLOCK_LINES,
)


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

