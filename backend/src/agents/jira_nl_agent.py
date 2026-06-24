"""
Jira NL Agent
Lets a person drive real Jira operations with plain-English instructions
(e.g. "comment on PROJ-12 saying the fix is deployed", "move PROJ-7 to Done",
"create a bug ticket for the login crash").

Unlike JiraAdapter (which calls Jira's REST API directly for the fixed,
deterministic skill pipelines), this talks to the actual Jira MCP server
(sooperset/mcp-atlassian) and gives Gemini direct tool access via function
calling, since here the *choice* of which Jira action to take is itself the
thing being delegated to the model — that's what MCP's dynamic tool
discovery is for.

Safety model: read-only tools (per the MCP server's own `readOnlyHint`
annotation, or a name-prefix fallback if a tool doesn't declare one) execute
immediately. Anything else — creating issues, transitioning status, adding
comments, deleting, etc. — is staged as a "pending_confirmation" and only
actually runs against Jira once a human calls confirm(session_id, approve=True).
A run can pause for confirmation multiple times in a row if the instruction
implies several write steps (e.g. "create a bug ticket, then assign it to me").

Each in-progress run keeps a live MCP subprocess + ClientSession open in
memory between the initial run() call and the matching confirm() call,
since HTTP requests are stateless but the MCP connection isn't. Sessions
are cleaned up on completion, decline, error, or after SESSION_TTL_SECONDS
of inactivity (use reap_expired_sessions() on a periodic task if you want
those cleaned up even when nobody ever calls confirm()).
"""

"""
Jira NL Agent
Lets a person drive real Jira operations with plain-English instructions
(e.g. "comment on PROJ-12 saying the fix is deployed", "move PROJ-7 to Done",
"create a bug ticket for the login crash").

Unlike JiraAdapter (which calls Jira's REST API directly for the fixed,
deterministic skill pipelines), this talks to the actual Jira MCP server
(sooperset/mcp-atlassian) and gives Gemini direct tool access via function
calling, since here the *choice* of which Jira action to take is itself the
thing being delegated to the model — that's what MCP's dynamic tool
discovery is for.

Safety model: read-only tools (per the MCP server's own `readOnlyHint`
annotation, or a name-prefix fallback if a tool doesn't declare one) execute
immediately. Anything else — creating issues, transitioning status, adding
comments, deleting, etc. — is staged as a "pending_confirmation" and only
actually runs against Jira once a human calls confirm(session_id, approve=True).
A run can pause for confirmation multiple times in a row if the instruction
implies several write steps (e.g. "create a bug ticket, then assign it to me").

Concurrency model: anyio/MCP's stdio_client ties its cancel scope to the
asyncio task that opened it, and will raise
"Attempted to exit cancel scope in a different task than it was entered in"
if anything tries to close it from a different task. Since run() and the
matching confirm() arrive as two separate HTTP requests (i.e. two separate
tasks), the MCP connection for a session is owned and closed entirely by one
dedicated background task (_session_worker) that lives for the session's
whole lifetime. run()/confirm() never touch the connection directly — they
hand a request to that worker over an asyncio.Queue and await its response
on a per-call future. This keeps every stdio_client open/close on the same
task, regardless of which HTTP request triggered it.

Sessions are cleaned up on completion, decline, error, or after
SESSION_TTL_SECONDS of inactivity (use reap_expired_sessions() on a
periodic task if you want those cleaned up even when nobody ever calls
confirm()).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Fallback only — used when a tool provides no readOnlyHint annotation at
# all. Anything matching these prefixes is treated as safe to auto-run;
# everything else defaults to requiring confirmation (deny-by-default).
SAFE_READ_PREFIXES = ("get_", "list_", "search_", "jira_get", "jira_search", "jira_list")

SESSION_TTL_SECONDS = 15 * 60  # abandoned pending confirmations expire
MAX_STEPS_PER_RUN = 15  # guards against a runaway tool-call loop
WORKER_STARTUP_TIMEOUT = 30  # seconds to wait for the MCP subprocess to come up

SYSTEM_PROMPT = (
    "You are a Jira operations assistant with direct tool access to a real "
    "Jira site via the Jira MCP server. Use the available tools to carry "
    "out the user's instruction, calling read-only tools as needed to "
    "gather context (e.g. look up an issue or project) before acting. Call "
    "exactly one tool at a time and wait for its result before deciding the "
    "next step. When the instruction is fully carried out (or you need "
    "information the user must supply, such as a project key you can't "
    "infer), reply with a short, plain-English summary — do not call any "
    "more tools at that point."
)


def _is_read_only(tool) -> bool:
    """True if this MCP tool is safe to auto-execute without confirmation."""
    hint = getattr(tool.annotations, "readOnlyHint", None) if tool.annotations else None
    if hint is not None:
        return bool(hint)
    return tool.name.startswith(SAFE_READ_PREFIXES)


@dataclass
class _AgentSession:
    """
    Everything _advance()/confirm() need, EXCEPT the live MCP connection
    itself — that lives only inside the worker task (see _session_worker)
    and is never touched from any other task.
    """
    worker_task: asyncio.Task
    call_queue: "asyncio.Queue[tuple[str, dict, asyncio.Future]]"
    gemini_tool: types.Tool
    history: list  # list[types.Content]
    pending_call: Optional[types.FunctionCall] = None
    pending_response_content: Optional[types.Content] = None
    created_at: float = field(default_factory=time.time)
    steps_taken: int = 0


class JiraNLAgent:
    MODEL = "gemini-3.5-flash"

    def __init__(
        self,
        jira_base_url: str,
        jira_email: str,
        jira_api_token: str,
        gemini_api_key: str,
        audit_log=None,
        mcp_transport: str = "docker",
        mcp_binary_path: str | None = None,
        confluence_base_url: str | None = None,
    ):
        self._jira_base_url = jira_base_url
        self._jira_email = jira_email
        self._jira_api_token = jira_api_token
        self._client = genai.Client(api_key=gemini_api_key)
        self._audit_log = audit_log
        self._mcp_transport = mcp_transport
        self._mcp_binary_path = mcp_binary_path
        self._confluence_base_url = confluence_base_url
        self._sessions: dict[str, _AgentSession] = {}
        self._tool_lookup: dict[str, dict] = {}  # session_id -> {tool_name: tool}

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self, instruction: str, meta: dict | None = None) -> dict:
        """Start a new agent run for a natural-language instruction."""
        session_id = str(uuid.uuid4())
        call_queue: "asyncio.Queue[tuple[str, dict, asyncio.Future]]" = asyncio.Queue()
        ready: asyncio.Future = asyncio.get_running_loop().create_future()

        worker_task = asyncio.create_task(self._session_worker(session_id, call_queue, ready))

        try:
            tools = await asyncio.wait_for(ready, timeout=WORKER_STARTUP_TIMEOUT)
        except (asyncio.TimeoutError, Exception) as e:
            worker_task.cancel()
            print(f"❌ Agent startup failed: {type(e).__name__}: {e}")
            raise RuntimeError(f"Failed to start Jira MCP connection: {e}") from e

        gemini_tool = self._build_gemini_tool(session_id, tools)
        history = [types.Content(role="user", parts=[types.Part(text=instruction)])]
        self._sessions[session_id] = _AgentSession(
            worker_task=worker_task, call_queue=call_queue,
            gemini_tool=gemini_tool, history=history,
        )

        self._log("JIRA_AGENT_STARTED", session_id, {"instruction": instruction, "meta": meta or {}})
        print(f"\n🤖 Jira NL Agent started (session: {session_id})")
        print(f"   Instruction: {instruction}")

        return await self._advance(session_id)

    async def confirm(self, session_id: str, approve: bool) -> dict:
        """Resume a run that's paused waiting on approval for a write action."""
        pending = self._sessions.get(session_id)
        if pending is None:
            return {"status": "error", "session_id": session_id, "error": "Unknown or expired session_id"}

        if time.time() - pending.created_at > SESSION_TTL_SECONDS:
            await self._cleanup(session_id)
            return {"status": "error", "session_id": session_id, "error": "Session expired — please start a new run"}

        call = pending.pending_call
        if call is None:
            return {"status": "error", "session_id": session_id, "error": "No pending action to confirm on this session"}

        # Replay the raw stored Content (not a reconstructed Part) so any
        # thought_signature attached to the original Gemini response survives.
        pending.history.append(pending.pending_response_content)

        if not approve:
            print(f"   🚫 Declined: {call.name}({call.args})")
            self._log("JIRA_AGENT_ACTION_DECLINED", session_id, {"tool": call.name, "args": call.args})
            pending.history.append(types.Content(
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
            self._log("JIRA_AGENT_ACTION_EXECUTED", session_id,
                       {"tool": call.name, "args": call.args, "result": result_payload})
            pending.history.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=call.name, response=result_payload)],
            ))

        pending.pending_call = None
        pending.pending_response_content = None
        return await self._advance(session_id)

    async def reap_expired_sessions(self) -> int:
        """Clean up abandoned sessions nobody ever confirmed/declined. Call periodically."""
        expired = [sid for sid, s in self._sessions.items() if time.time() - s.created_at > SESSION_TTL_SECONDS]
        for sid in expired:
            await self._cleanup(sid)
        return len(expired)

    # ── The session worker — the ONLY task that ever touches the live
    #    stdio_client/ClientSession for a given session_id ─────────────────

    async def _session_worker(self, session_id: str, call_queue: "asyncio.Queue", ready: "asyncio.Future") -> None:
        try:
            async with stdio_client(self._server_params()) as (read, write):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    tools_result = await mcp_session.list_tools()

                    if not ready.done():
                        ready.set_result(tools_result.tools)

                    while True:
                        item = await call_queue.get()
                        if item is None:  # shutdown signal
                            break
                        tool_name, tool_args, future = item
                        try:
                            tool_result = await mcp_session.call_tool(tool_name, tool_args)
                            if not future.done():
                                future.set_result(tool_result)
                        except Exception as e:
                            if not future.done():
                                future.set_exception(e)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            print(f"   ⚠️  Jira MCP worker for session {session_id} exited: {type(e).__name__}: {e}")
        finally:
            # Drain anything left in the queue so callers don't hang forever
            # if the worker died mid-flight.
            while not call_queue.empty():
                item = call_queue.get_nowait()
                if item is not None:
                    _, _, future = item
                    if not future.done():
                        future.set_exception(RuntimeError("Jira MCP session ended before this call completed"))

    async def _call_tool_via_worker(self, session_id: str, tool_name: str, tool_args: dict) -> dict:
        pending = self._sessions.get(session_id)
        if pending is None:
            raise RuntimeError("Session no longer exists")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await pending.call_queue.put((tool_name, tool_args, future))
        tool_result = await future
        return self._tool_result_to_dict(tool_result)

    # ── Internal loop ────────────────────────────────────────────────────

    async def _advance(self, session_id: str) -> dict:
        """Send the current history to Gemini and act on (or surface) what it proposes."""
        pending = self._sessions[session_id]

        if pending.steps_taken >= MAX_STEPS_PER_RUN:
            await self._cleanup(session_id)
            return {"status": "max_steps_exceeded", "session_id": session_id,
                    "error": f"Stopped after {MAX_STEPS_PER_RUN} tool calls without finishing."}
        pending.steps_taken += 1

        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=pending.history,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[pending.gemini_tool],
            ),
        )

        calls = response.function_calls or []
        if not calls:
            final_text = response.text or ""
            pending.history.append(types.Content(role="model", parts=[types.Part(text=final_text)]))
            self._log("JIRA_AGENT_COMPLETED", session_id, {"message": final_text})
            print(f"   🏁 Agent finished: {final_text}")
            await self._cleanup(session_id)
            return {"status": "completed", "session_id": session_id, "message": final_text}

        call = calls[0]  # one action at a time, by design (see SYSTEM_PROMPT)
        tool = self._tool_lookup.get(session_id, {}).get(call.name)

        if tool is not None and _is_read_only(tool):
            print(f"   🔎 Auto-running read-only tool: {call.name}({call.args})")
            # Use the raw response Content, not a reconstructed Part, so the
            # thought_signature (if any) is preserved when replayed to Gemini.
            pending.history.append(response.candidates[0].content)
            result_payload = await self._call_tool_via_worker(session_id, call.name, call.args or {})
            pending.history.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=call.name, response=result_payload)],
            ))
            return await self._advance(session_id)

        # Write action (or an unrecognized tool — fail safe, require confirmation either way).
        pending.pending_call = call
        pending.pending_response_content = response.candidates[0].content
        print(f"   ⏸  Awaiting confirmation: {call.name}({call.args})")
        self._log("JIRA_AGENT_ACTION_PROPOSED", session_id, {"tool": call.name, "args": call.args})
        return {
            "status": "pending_confirmation",
            "session_id": session_id,
            "proposed_action": {"tool": call.name, "args": call.args},
        }

    # ── Helpers ──────────────────────────────────────────────────────────

    def _server_params(self) -> StdioServerParameters:
        env = {
            "JIRA_URL": self._jira_base_url,
            "JIRA_USERNAME": self._jira_email,
            "JIRA_API_TOKEN": self._jira_api_token,
        }
        if self._confluence_base_url:
            env["CONFLUENCE_URL"] = self._confluence_base_url
            env["CONFLUENCE_USERNAME"] = self._jira_email
            env["CONFLUENCE_API_TOKEN"] = self._jira_api_token

        if self._mcp_transport == "binary":
            if not self._mcp_binary_path:
                raise RuntimeError("JIRA_MCP_BINARY_PATH must be set when JIRA_MCP_TRANSPORT=binary")
            return StdioServerParameters(command=self._mcp_binary_path, args=[], env=env)

        docker_env_flags = []
        for key in env:
            docker_env_flags += ["-e", key]

        return StdioServerParameters(
            command="docker",
            args=["run", "-i", "--rm", *docker_env_flags, "ghcr.io/sooperset/mcp-atlassian"],
            env=env,
        )

    def _build_gemini_tool(self, session_id: str, mcp_tools) -> types.Tool:
        self._tool_lookup[session_id] = {t.name: t for t in mcp_tools}
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description or "",
                parameters_json_schema=t.inputSchema,
            )
            for t in mcp_tools
        ]
        return types.Tool(function_declarations=declarations)

    @staticmethod
    def _tool_result_to_dict(tool_result) -> dict:
        text_parts = [c.text for c in tool_result.content if hasattr(c, "text")]
        payload: dict[str, Any] = {"text": "\n".join(text_parts)}
        if tool_result.isError:
            payload["error"] = True
        return payload

    async def _cleanup(self, session_id: str) -> None:
        pending = self._sessions.pop(session_id, None)
        self._tool_lookup.pop(session_id, None)
        if pending:
            # Signal the worker (its own task) to shut down; never touch
            # the stdio_client/ClientSession context managers directly here.
            await pending.call_queue.put(None)
            try:
                await asyncio.wait_for(pending.worker_task, timeout=10)
            except asyncio.TimeoutError:
                pending.worker_task.cancel()

    def _log(self, event_type: str, session_id: str, data: dict) -> None:
        if self._audit_log:
            self._audit_log.record({"type": event_type, "job_id": session_id, "skill_id": "jira-nl-agent", **data})
