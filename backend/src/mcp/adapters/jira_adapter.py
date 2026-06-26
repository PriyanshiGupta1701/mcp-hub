"""
Jira MCP Adapter
Connects to Jira REST API: fetch issues, search, post comments.
"""

import base64
import httpx
from typing import Any


class JiraAdapter:
    def __init__(self, base_url: str, email: str, api_token: str):
        if not all([base_url, email, api_token]):
            raise ValueError("Jira requires base_url, email, and api_token")
        self._base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def call(self, action: str, params: dict) -> Any:
        handlers = {
            "getIssue":        self._get_issue,
            "searchIssues":    self._search_issues,
            "addComment":      self._add_comment,
            "getProjectIssues": self._get_project_issues,
        }
        handler = handlers.get(action)
        if not handler:
            raise ValueError(f"Unknown Jira action: {action}")
        return await handler(params)

    async def _get_issue(self, p: dict) -> dict:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self._base_url}/rest/api/3/issue/{p['issueKey']}",
                headers=self._headers,
            )
            r.raise_for_status()
            d = r.json()
            fields = d["fields"]
            desc = ""
            try:
                desc = fields["description"]["content"][0]["content"][0]["text"]
            except (TypeError, KeyError, IndexError):
                pass
            return {
                "key":      d["key"],
                "summary":  fields["summary"],
                "description": desc,
                "status":   fields["status"]["name"],
                "priority": fields.get("priority", {}).get("name"),
                "assignee": (fields.get("assignee") or {}).get("displayName"),
                "reporter": (fields.get("reporter") or {}).get("displayName"),
                "labels":   fields.get("labels", []),
                "created":  fields["created"],
                "updated":  fields["updated"],
            }

    async def _search_issues(self, p: dict) -> list:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self._base_url}/rest/api/3/search",
                headers=self._headers,
                params={"jql": p["jql"], "maxResults": p.get("maxResults", 20)},
            )
            r.raise_for_status()
            return [
                {
                    "key":      i["key"],
                    "summary":  i["fields"]["summary"],
                    "status":   i["fields"]["status"]["name"],
                    "priority": (i["fields"].get("priority") or {}).get("name"),
                }
                for i in r.json().get("issues", [])
            ]

    async def _add_comment(self, p: dict) -> dict:
        body = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": p["body"]}]}],
            }
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self._base_url}/rest/api/3/issue/{p['issueKey']}/comment",
                headers=self._headers,
                json=body,
            )
            r.raise_for_status()
            return r.json()

    async def _get_project_issues(self, p: dict) -> list:
        jql = f"project = {p['projectKey']}"
        if p.get("status"):
            jql += f" AND status = \"{p['status']}\""
        return await self._search_issues({"jql": jql})
