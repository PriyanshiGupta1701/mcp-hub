"""
GitHub MCP Adapter (real MCP, not REST)
Connects to the actual GitHub MCP server (ghcr.io/github/github-mcp-server)
over stdio — the same server your GitHubNLAgent talks to — instead of
calling the GitHub REST API directly with httpx.

Why this exists: the original GitHubAdapter in this folder is named "MCP"
but is really just an httpx wrapper around api.github.com. This adapter is
the genuine article — your skills (code_review.py, etc.) get PR data by
asking the GitHub MCP server's own tools, which means:
  - one auth path (the MCP server's GITHUB_PERSONAL_ACCESS_TOKEN) for both
    your fixed skills AND your NL agent
  - if GitHub adds/changes tools, you get them for free without touching
    this file
  - your skill code (ctx.mcp_client.call("github", "getPRFiles", ...))
    doesn't need to change — this adapter keeps the same action names and
    translates them into the MCP server's real tool calls underneath

Connection model: unlike GitHubNLAgent (which opens a fresh MCP subprocess
per agent run, since each run is short-lived and one-shot), this adapter
keeps ONE long-lived MCP connection open for the lifetime of the app,
since skills may be invoked frequently and repeatedly. Call start() once
at startup and stop() once at shutdown.
"""

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class GitHubMCPAdapter:
    def __init__(self, token: str, transport: str = "docker", binary_path: str | None = None):
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._transport = transport
        self._binary_path = binary_path
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the persistent MCP connection. Call once at app startup."""
        self._exit_stack = AsyncExitStack()
        read, write = await self._exit_stack.enter_async_context(stdio_client(self._server_params()))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        print("🔌 GitHub MCP adapter connected (real MCP server)")

    async def stop(self) -> None:
        """Close the persistent MCP connection. Call once at app shutdown."""
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    def _server_params(self) -> StdioServerParameters:
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": self._token}
        if self._transport == "binary":
            if not self._binary_path:
                raise RuntimeError("binary_path required when transport='binary'")
            return StdioServerParameters(command=self._binary_path, args=[], env=env)
        return StdioServerParameters(
            command="docker",
            args=["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
            env=env,
        )

    # ── Same call() shape your skills already use ───────────────────────

    async def call(self, action: str, params: dict) -> Any:
        if self._session is None:
            raise RuntimeError("GitHubMCPAdapter.start() must be called before use")

        handlers = {
            "getPullRequest": self._get_pull_request,
            "getPRDiff":      self._get_pr_diff,
            "getPRFiles":     self._get_pr_files,
            "postComment":    self._post_comment,
            "getPRCommits":   self._get_pr_commits,
        }
        handler = handlers.get(action)
        if not handler:
            raise ValueError(f"Unknown GitHub action: {action}")
        return await handler(params)

    # ── Translate each action into the real MCP server's tool calls ────
    # Tool names below match the actual github-mcp-server toolset (pulls).
    # If GitHub renames a tool in a future release, this is the only place
    # that needs updating — skill code is unaffected.

    async def _call_tool(self, name: str, args: dict) -> Any:
        result = await self._session.call_tool(name, args)
        if result.isError:
            text = " ".join(c.text for c in result.content if hasattr(c, "text"))
            raise RuntimeError(f"GitHub MCP tool '{name}' failed: {text}")
        # github-mcp-server tools return JSON (or sometimes raw diff text)
        # as a single text content block.
        import json
        text = "".join(c.text for c in result.content if hasattr(c, "text"))
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # get_diff returns a raw unified-diff string, not JSON.
            return text

    async def _get_pull_request(self, p: dict) -> dict:
        d = await self._call_tool("pull_request_read", {
            "method": "get", "owner": p["owner"], "repo": p["repo"], "pullNumber": p["pullNumber"],
        })
        return {
            "number":       d["number"],
            "title":        d["title"],
            "body":         d.get("body", ""),
            "author":       d["user"]["login"],
            "baseBranch":   d["base"]["ref"],
            "headBranch":   d["head"]["ref"],
            "state":        d["state"],
            "additions":    d.get("additions", 0),
            "deletions":    d.get("deletions", 0),
            "changedFiles": d.get("changed_files", 0),
            "createdAt":    d["created_at"],
            "url":          d["html_url"],
        }

    async def _get_pr_diff(self, p: dict) -> dict:
        d = await self._call_tool("pull_request_read", {
            "method": "get_diff", "owner": p["owner"], "repo": p["repo"], "pullNumber": p["pullNumber"],
        })
        return {"diff": d if isinstance(d, str) else d.get("diff", "")}

    async def _get_pr_files(self, p: dict) -> list:
        files = await self._call_tool("pull_request_read", {
            "method": "get_files", "owner": p["owner"], "repo": p["repo"], "pullNumber": p["pullNumber"],
            "perPage": p.get("perPage", 100),
        })
        items = files if isinstance(files, list) else files.get("files", files.get("nodes", []))
        return [
            {
                "filename":  f.get("filename") or f.get("path", ""),
                "status":    f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch":     f.get("patch", ""),
            }
            for f in items
        ]

    async def _post_comment(self, p: dict) -> dict:
        return await self._call_tool("add_issue_comment", {
            "owner": p["owner"], "repo": p["repo"],
            "issue_number": p["pullNumber"], "body": p["body"],
        })

    async def _get_pr_commits(self, p: dict) -> list:
        commits = await self._call_tool("pull_request_read", {
            "method": "get_commits", "owner": p["owner"], "repo": p["repo"], "pullNumber": p["pullNumber"],
            "perPage": p.get("perPage", 100),
        })
        items = commits if isinstance(commits, list) else commits.get("commits", commits.get("nodes", []))
        return [
            {
                "sha":     c.get("sha") or c.get("oid", ""),
                "message": (c.get("commit") or {}).get("message") or c.get("message", ""),
                "author":  ((c.get("commit") or {}).get("author") or {}).get("name") or c.get("author", ""),
            }
            for c in items
        ]
