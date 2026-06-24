"""
GitHub NL Agent
Lets a person drive real GitHub operations with plain-English instructions.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SAFE_READ_PREFIXES = ("get_", "list_", "search_")

SESSION_TTL_SECONDS = 15 * 60
MAX_STEPS_PER_RUN = 15
WORKER_STARTUP_TIMEOUT = 30


def _enrich_instruction(instruction: str, github_username: str) -> str:
    """
    Append the authenticated username as explicit context on EVERY call,
    unconditionally — no self-reference detection, no regex heuristics that
    can produce false positives/negatives (e.g. "of repo" previously
    matching a "names another owner" pattern and suppressing injection
    entirely). The model is told plainly: explicit owner in the instruction
    wins; otherwise assume this user. This removes the failure mode where
    no owner was ever injected and Gemini silently guessed one.
    """
    return (
        f"{instruction}\n\n"
        f"[Context: the authenticated GitHub user making this request is "
        f"'{github_username}'. If no repository owner is explicitly stated "
        f"above, assume the repository belongs to '{github_username}'. If "
        f"an owner IS explicitly stated (e.g. 'someuser/some-repo', 'in "
        f"octocat's repo', 'for the acme org'), use that owner instead — "
        f"do not override an explicitly named owner.]"
    )


def _build_system_prompt(github_username: str, tool_directory: str) -> str:
    """
    Inject the authenticated user's GitHub login, an explicit owner-
    resolution rule, and the tool directory into the system prompt.
    """
    return f"""You are a precise GitHub assistant.

Authenticated GitHub user: {github_username}

## Owner resolution — follow exactly, every time, no exceptions
Every GitHub tool call needs a repository "owner" (a username or org name).

1. If the instruction explicitly names a different owner
   (e.g. "octocat/Hello-World", "in the acme org's repo", "for user torvalds"),
   use that owner.
2. Otherwise — including when a repo is mentioned with NO owner at all,
   like "repo mcp-hub" or "the billing-service repo" — the owner is
   "{github_username}". This applies whether or not the instruction uses
   self-referential words like "my" or "I".
3. NEVER invent, guess, or default to any other username. If you cannot
   resolve the owner using rules 1-2, stop and ask the user instead of
   calling a tool with a guessed owner.

## Available MCP tools
{tool_directory}

## Tool selection rules
1. Read the tool directory above.
2. Choose ONE tool at a time; wait for its result before the next step.
3. Prefer the most specific tool that matches the action and resource.
4. Never fabricate a parameter value (owner, repo, PR number, etc.) — if a
   required value is missing and cannot be resolved by the owner-resolution
   rule above, ask the user instead of guessing.
5. Do not repeat a tool call with identical arguments. If a call errors or
   returns nothing useful, stop and explain — do not retry blindly.

## Execution
- Read first, then act.
- Once the task is complete, give a short plain-English summary and stop.
"""


def _build_tool_directory(mcp_tools) -> str:
    """Convert MCP tools into a compact directory Gemini reads before choosing tools."""
    lines = []
    for t in sorted(mcp_tools, key=lambda x: x.name):
        desc = (t.description or "").strip()
        if len(desc) > 180:
            desc = desc[:180] + "..."
        read_only = getattr(getattr(t, "annotations", None), "readOnlyHint", None)
        mode = "READ" if read_only else "WRITE"
        lines.append(f"- {t.name} [{mode}] → {desc}")
    return "\n".join(lines)


def _is_read_only(tool) -> bool:
    hint = getattr(tool.annotations, "readOnlyHint", None) if tool.annotations else None
    if hint is not None:
        return bool(hint)
    return tool.name.startswith(SAFE_READ_PREFIXES)


async def _resolve_github_username(token: str) -> str:
    """Call GET /user with the PAT to get the real authenticated GitHub login."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10,
            )
            r.raise_for_status()
            login = r.json().get("login", "unknown")
            print(f"🔑 GitHub authenticated user: {login}")
            return login
    except Exception as e:
        print(f"⚠️  Could not resolve GitHub username from token: {e}")
        return "unknown"


@dataclass
class _AgentSession:
    worker_task: asyncio.Task
    call_queue: "asyncio.Queue"
    gemini_tool: types.Tool
    history: list
    system_prompt: str
    pending_call: Optional[types.FunctionCall] = None
    pending_response_content: Optional[types.Content] = None
    created_at: float = field(default_factory=time.time)
    steps_taken: int = 0
    tool_call_counts: dict = field(default_factory=dict)


class GitHubNLAgent:
    MODEL = "gemini-3.1-flash-lite"

    def __init__(
        self,
        github_token: str,
        gemini_api_key: str,
        audit_log=None,
        mcp_transport: str = "docker",
        mcp_binary_path: str | None = None,
        github_toolsets: str = "repos,issues,pull_requests",
    ):
        self._github_token = github_token
        self._client = genai.Client(api_key=gemini_api_key)
        self._audit_log = audit_log
        self._mcp_transport = mcp_transport
        self._mcp_binary_path = mcp_binary_path
        self._github_toolsets = github_toolsets
        self._sessions: dict[str, _AgentSession] = {}
        self._tool_lookup: dict[str, dict] = {}
        self._github_username: str = "unknown"

    async def initialize(self) -> None:
        """Resolve the authenticated GitHub username from the PAT token. Call once at app startup."""
        self._github_username = await _resolve_github_username(self._github_token)

    async def run(self, instruction: str, meta: dict | None = None) -> dict:
        if self._github_username == "unknown":
            self._github_username = await _resolve_github_username(self._github_token)

        session_id = str(uuid.uuid4())
        call_queue: asyncio.Queue = asyncio.Queue()
        ready: asyncio.Future = asyncio.get_running_loop().create_future()

        worker_task = asyncio.create_task(self._session_worker(session_id, call_queue, ready))

        try:
            tools = await asyncio.wait_for(ready, timeout=WORKER_STARTUP_TIMEOUT)
        except (asyncio.TimeoutError, Exception) as e:
            worker_task.cancel()
            raise RuntimeError(f"Failed to start GitHub MCP connection: {e}") from e

        tool_directory = _build_tool_directory(tools)
        system_prompt = _build_system_prompt(self._github_username, tool_directory)
        gemini_tool = self._build_gemini_tool(session_id, tools)

        # Always inject the username — unconditionally, every call. See
        # module docstring for why the old self-reference-detection approach
        # was unsafe.
        enriched_instruction = _enrich_instruction(instruction, self._github_username)
        history = [types.Content(role="user", parts=[types.Part(text=enriched_instruction)])]

        self._sessions[session_id] = _AgentSession(
            worker_task=worker_task,
            call_queue=call_queue,
            gemini_tool=gemini_tool,
            history=history,
            system_prompt=system_prompt,
        )

        self._log("GITHUB_AGENT_STARTED", session_id, {"instruction": instruction, "meta": meta or {}})
        print(f"\n🤖 GitHub NL Agent started (session: {session_id})")
        print(f"   User: {self._github_username}")
        print(f"   Instruction: {instruction}")

        return await self._advance(session_id)

    async def confirm(self, session_id: str, approve: bool) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            return {"status": "error", "session_id": session_id, "error": "Unknown or expired session_id"}

        if time.time() - session.created_at > SESSION_TTL_SECONDS:
            await self._cleanup(session_id)
            return {"status": "error", "session_id": session_id, "error": "Session expired — please start a new run"}

        call = session.pending_call
        if call is None:
            return {"status": "error", "session_id": session_id, "error": "No pending action to confirm"}

        session.history.append(session.pending_response_content)

        if not approve:
            print(f"   🚫 Declined: {call.name}({call.args})")
            self._log("GITHUB_AGENT_ACTION_DECLINED", session_id, {"tool": call.name, "args": call.args})
            session.history.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name=call.name,
                    response={"error": "The user declined this action. Do not retry it — "
                                       "either propose a different approach or ask what they'd like instead."},
                )],
            ))
        else:
            print(f"   ✅ Confirmed — executing: {call.name}({call.args})")
            result_payload = await self._call_tool_via_worker(session_id, call.name, call.args or {})
            self._log("GITHUB_AGENT_ACTION_EXECUTED", session_id,
                      {"tool": call.name, "args": call.args, "result": result_payload})
            session.history.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=call.name, response=result_payload)],
            ))

        session.pending_call = None
        session.pending_response_content = None
        return await self._advance(session_id)

    async def reap_expired_sessions(self) -> int:
        expired = [sid for sid, s in self._sessions.items()
                   if time.time() - s.created_at > SESSION_TTL_SECONDS]
        for sid in expired:
            await self._cleanup(sid)
        return len(expired)

    async def _session_worker(self, session_id: str, call_queue: asyncio.Queue, ready: asyncio.Future) -> None:
        try:
            async with stdio_client(self._server_params()) as (read, write):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    tools_result = await mcp_session.list_tools()
                    if not ready.done():
                        ready.set_result(tools_result.tools)
                    while True:
                        item = await call_queue.get()
                        if item is None:
                            break
                        tool_name, tool_args, future = item
                        try:
                            result = await mcp_session.call_tool(tool_name, tool_args)
                            if not future.done():
                                future.set_result(result)
                        except Exception as e:
                            if not future.done():
                                future.set_exception(e)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            print(f"   ⚠️  MCP worker for {session_id} exited: {e}")
        finally:
            while not call_queue.empty():
                item = call_queue.get_nowait()
                if item is not None:
                    _, _, future = item
                    if not future.done():
                        future.set_exception(RuntimeError("MCP session ended"))

    async def _call_tool_via_worker(self, session_id: str, tool_name: str, tool_args: dict) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError("Session no longer exists")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await session.call_queue.put((tool_name, tool_args, future))
        return self._tool_result_to_dict(await future)

    async def _advance(self, session_id: str) -> dict:
        session = self._sessions[session_id]

        if session.steps_taken >= MAX_STEPS_PER_RUN:
            await self._cleanup(session_id)
            return {"status": "max_steps_exceeded", "session_id": session_id,
                    "error": f"Stopped after {MAX_STEPS_PER_RUN} tool calls without finishing."}
        session.steps_taken += 1

        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=session.history,
            config=types.GenerateContentConfig(
                system_instruction=session.system_prompt,
                tools=[session.gemini_tool],
            ),
        )

        calls = response.function_calls or []
        if not calls:
            final_text = response.text or ""
            session.history.append(types.Content(role="model", parts=[types.Part(text=final_text)]))
            self._log("GITHUB_AGENT_COMPLETED", session_id, {"message": final_text})
            print(f"   🏁 Agent finished")
            await self._cleanup(session_id)
            return {
                "status": "completed",
                "session_id": session_id,
                "github_user": self._github_username,
                "message": final_text,
            }

        call = calls[0]
        tool = self._tool_lookup.get(session_id, {}).get(call.name)

        if tool is not None and _is_read_only(tool):
            call_key = (call.name, str(sorted((call.args or {}).items())))
            session.tool_call_counts[call_key] = session.tool_call_counts.get(call_key, 0) + 1
            if session.tool_call_counts[call_key] > 1:
                print(f"   🔁 Loop detected: {call.name} called with identical args "
                      f"{session.tool_call_counts[call_key]}x — stopping")
                loop_msg = (
                    f"I tried calling {call.name} multiple times with the same arguments and "
                    f"got the same result. I cannot complete the task this way. "
                    f"Please check that the repository exists and try rephrasing your instruction."
                )
                session.history.append(types.Content(role="model", parts=[types.Part(text=loop_msg)]))
                await self._cleanup(session_id)
                return {
                    "status": "completed",
                    "session_id": session_id,
                    "github_user": self._github_username,
                    "message": loop_msg,
                }

            print(f"   🔎 Auto-running read-only tool: {call.name}({call.args})")
            session.history.append(response.candidates[0].content)
            result_payload = await self._call_tool_via_worker(session_id, call.name, call.args or {})
            session.history.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=call.name, response=result_payload)],
            ))
            return await self._advance(session_id)

        session.pending_call = call
        session.pending_response_content = response.candidates[0].content
        print(f"   ⏸  Awaiting confirmation: {call.name}({call.args})")
        self._log("GITHUB_AGENT_ACTION_PROPOSED", session_id, {"tool": call.name, "args": call.args})
        return {
            "status": "pending_confirmation",
            "session_id": session_id,
            "github_user": self._github_username,
            "proposed_action": {"tool": call.name, "args": call.args},
        }

    def _server_params(self) -> StdioServerParameters:
        if self._mcp_transport == "binary":
            if not self._mcp_binary_path:
                raise RuntimeError("GITHUB_MCP_BINARY_PATH must be set when GITHUB_MCP_TRANSPORT=binary")
            return StdioServerParameters(
                command=self._mcp_binary_path,
                args=["stdio"],
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": self._github_token,
                     "GITHUB_TOOLSETS": self._github_toolsets},
            )
        return StdioServerParameters(
            command="docker",
            args=["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                  "-e", "GITHUB_TOOLSETS", "ghcr.io/github/github-mcp-server"],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": self._github_token,
                 "GITHUB_TOOLSETS": self._github_toolsets},
        )

    def _build_gemini_tool(self, session_id: str, mcp_tools) -> types.Tool:
        self._tool_lookup[session_id] = {t.name: t for t in mcp_tools}
        return types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t.name,
                description=t.description or "",
                parameters_json_schema=t.inputSchema,
            )
            for t in mcp_tools
        ])

    @staticmethod
    def _tool_result_to_dict(tool_result) -> dict:
        text_parts = [c.text for c in tool_result.content if hasattr(c, "text")]
        payload: dict[str, Any] = {"text": "\n".join(text_parts)}
        if tool_result.isError:
            payload["error"] = True
        return payload

    async def _cleanup(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        self._tool_lookup.pop(session_id, None)
        if session:
            await session.call_queue.put(None)
            try:
                await asyncio.wait_for(session.worker_task, timeout=10)
            except asyncio.TimeoutError:
                session.worker_task.cancel()

    def _log(self, event_type: str, session_id: str, data: dict) -> None:
        if self._audit_log:
            self._audit_log.record({
                "type": event_type, "job_id": session_id,
                "skill_id": "github-nl-agent", **data,
            })
