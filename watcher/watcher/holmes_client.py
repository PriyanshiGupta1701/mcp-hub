"""
holmes_client.py
-----------------
Talks to Holmes over HTTP, with retry handling for the known transient
failure modes (timeouts, 429s, and the "analysis: None" 500 bug).
"""

import time

import requests

from .config import HOLMES_URL, HOLMES_TIMEOUT_SEC


# def ask_holmes(question, model=None, max_attempts=6, retry_wait_sec=60):
#     """POST to Holmes, retrying up to max_attempts times (retry_wait_sec apart)
#     on the known Gemini 'analysis: None' 500 error (model finished tool calls
#     but produced no summary), on 429s, and on read timeouts. Every retry
#     re-runs the full investigation from scratch — Holmes's API is stateless,
#     there's no way to resume a partial tool-call chain — so this can get
#     expensive: worst case is max_attempts * (HOLMES_TIMEOUT_SEC + retry_wait_sec).
#     Pass model= to override Holmes's default (e.g. for prompts too complex
#     for the lite model to reliably produce any output for at all)."""
#     payload = {"ask": question}
#     if model:
#         payload["model"] = model
#     last_error = None
#     for attempt in range(max_attempts):
#         try:
#             resp = requests.post(HOLMES_URL, json=payload, timeout=HOLMES_TIMEOUT_SEC)
#         except requests.exceptions.ReadTimeout:
#             last_error = f"Holmes timed out after {HOLMES_TIMEOUT_SEC}s"
#             print(f"[watcher] {last_error} (attempt {attempt + 1}/{max_attempts})")
#             if attempt < max_attempts - 1:
#                 time.sleep(retry_wait_sec)
#                 continue
#             break

#         if resp.status_code == 429:
#             last_error = "429 rate limited"
#             print(f"[watcher] Rate limited by Holmes (attempt {attempt + 1}/{max_attempts}), waiting {retry_wait_sec}s")
#             if attempt < max_attempts - 1:
#                 time.sleep(retry_wait_sec)
#                 continue
#             break

#         if resp.status_code == 500:
#             body_text = resp.text or ""
#             if "analysis" in body_text and ("NoneType" in body_text or "None" in body_text):
#                 last_error = "Holmes repeatedly returned no summary (analysis: None)"
#                 print(f"[watcher] Holmes returned no summary (attempt {attempt + 1}/{max_attempts}), retrying in {retry_wait_sec}s")
#                 if attempt < max_attempts - 1:
#                     time.sleep(retry_wait_sec)
#                     continue
#                 break
#             # Some other 500 — not the known retryable case, fail fast rather
#             # than burn 6 retries on a bug retrying can't fix.
#             resp.raise_for_status()

#         resp.raise_for_status()
#         data = resp.json()
#         return data.get("analysis") or ""

#     raise RuntimeError(last_error or f"ask_holmes failed after {max_attempts} attempts")

def ask_holmes(question, model=None, max_attempts=6, retry_wait_sec=60):
    conversation_history = None
    last_error = None
    was_rate_limited = False

    for attempt in range(max_attempts):
        if conversation_history is None:
            ask_text = question
        elif was_rate_limited:
            # Rate-limited mid-investigation: resume with tools allowed
            ask_text = (
                "Continue the investigation from where you left off. "
                "Use tools as needed to complete gathering the data, "
                "then provide your final answer."
            )
        else:
            # Finished tools but no summary: skip re-running tools
            ask_text = (
                "Continue. You already gathered the evidence above — "
                "do not re-run any tools. Just produce the final structured summary now."
            )

        payload = {"ask": ask_text}
        if conversation_history is not None:
            payload["conversation_history"] = conversation_history
        if model:
            payload["model"] = model

        try:
            resp = requests.post(HOLMES_URL, json=payload, timeout=HOLMES_TIMEOUT_SEC)
        except requests.exceptions.ReadTimeout:
            last_error = f"Holmes timed out after {HOLMES_TIMEOUT_SEC}s"
            print(f"[watcher] {last_error} (attempt {attempt + 1}/{max_attempts})")
            was_rate_limited = False
            if attempt < max_attempts - 1:
                time.sleep(retry_wait_sec)
            continue

        if resp.status_code == 429:
            last_error = "429 rate limited"
            print(f"[watcher] Rate limited (attempt {attempt + 1}/{max_attempts}), waiting {retry_wait_sec}s")
            was_rate_limited = True
            if attempt < max_attempts - 1:
                time.sleep(retry_wait_sec)
            continue

        if resp.status_code == 500:
            body_text = resp.text or ""
            if "analysis" in body_text and ("NoneType" in body_text or "None" in body_text):
                last_error = "Holmes returned no summary (analysis: None)"
                was_rate_limited = False
                if attempt < max_attempts - 1:
                    time.sleep(retry_wait_sec)
                continue
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        analysis = data.get("analysis") or ""

        if not analysis and data.get("conversation_history"):
            conversation_history = data["conversation_history"]
            rate_limited = (data.get("metadata") or {}).get("rate_limited")
            was_rate_limited = bool(rate_limited)
            if rate_limited:
                last_error = "Holmes was rate-limited mid-investigation"
                # Wait longer before retrying a rate limit — Gemini needs cooldown
                wait = retry_wait_sec * 2
            else:
                last_error = "Holmes finished tools but produced no summary"
                wait = retry_wait_sec
            print(f"[watcher] {last_error} — resuming from {len(conversation_history)} saved messages "
                  f"(attempt {attempt + 1}/{max_attempts}), waiting {wait}s")
            if attempt < max_attempts - 1:
                time.sleep(wait)
            continue

        return analysis

    raise RuntimeError(last_error or f"ask_holmes failed after {max_attempts} attempts")