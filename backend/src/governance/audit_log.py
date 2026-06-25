"""
Audit Log
Records every skill execution for governance, compliance, and debugging.
In production, swap the in-memory list for a database (PostgreSQL, etc.)
"""

import uuid
from datetime import datetime, timezone


class AuditLog:
    def __init__(self):
        self._entries: list = []

    def record(self, event: dict) -> dict:
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self._entries.append(entry)
        return entry

    def query(
        self,
        skill_id: str = None,
        event_type: str = None,
        since: str = None,
        limit: int = 100,
    ) -> list:
        results = list(self._entries)
        if skill_id:
            results = [e for e in results if e.get("skill_id") == skill_id]
        if event_type:
            results = [e for e in results if e.get("type") == event_type]
        if since:
            since_dt = datetime.fromisoformat(since)
            results = [e for e in results if datetime.fromisoformat(e["timestamp"]) >= since_dt]
        return list(reversed(results[-limit:]))

    def get_summary(self) -> dict:
        by_skill: dict = {}
        by_status = {"SKILL_COMPLETED": 0, "SKILL_FAILED": 0, "SKILL_STARTED": 0}

        for entry in self._entries:
            if sid := entry.get("skill_id"):
                by_skill[sid] = by_skill.get(sid, 0) + 1
            if entry.get("type") in by_status:
                by_status[entry["type"]] += 1

        return {
            "total_events": len(self._entries),
            "by_skill": by_skill,
            "by_status": by_status,
        }
