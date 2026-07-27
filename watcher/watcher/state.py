"""
state.py
--------
Persisted watcher state (which issues have already been alerted on) plus
small stateless helpers used across all checks: parsing Holmes's JSON
replies and computing issue signatures for dedup.
"""

import hashlib
import json
import os
import re

from .config import STATE_FILE

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



def issue_signature(category, summary):
    return hashlib.sha256(f"{category}:{summary}".encode()).hexdigest()[:16]
