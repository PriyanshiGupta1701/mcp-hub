"""
FastAPI Server
REST API to trigger skills, run pipelines, query the registry, and view audit logs.
Includes a GitHub webhook endpoint for automatic PR-triggered pipelines.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Request models ─────────────────────────────────────────────────────────────

class RunSkillRequest(BaseModel):
    input: dict
    meta: dict = {}

class RunPipelineRequest(BaseModel):
    skill_ids: list[str]
    input: dict
    meta: dict = {}

class RunAgentRequest(BaseModel):
    instruction: str
    meta: dict = {}

class ConfirmAgentRequest(BaseModel):
    session_id: str
    approve: bool


# ── Factory ────────────────────────────────────────────────────────────────────

def create_app(
    registry,
    orchestrator,
    audit_log,
    github_agent=None,
    jira_agent=None,
    github_mcp_adapter=None,
    cors_origins: list = None,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if github_mcp_adapter is not None:
            await github_mcp_adapter.start()
        # Resolve the GitHub username from the PAT token once at startup
        # so every agent session already knows who "me" refers to.
        if github_agent is not None:
            await github_agent.initialize()
        print("🚀 Enterprise Skills Platform API ready")
        yield
        if github_mcp_adapter is not None:
            await github_mcp_adapter.stop()

    app = FastAPI(
        title="Enterprise Skills Platform",
        description="MCP-Powered AI automation for engineering workflows",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ── CORS ───────────────────────────────────────────────────────────────────
    # cors_origins from env (see main.py). Falls back to localhost:3000 / 5173
    # so a local React/Vite dev server works out of the box without any config.
    origins = cors_origins or ["http://localhost:3000", "http://localhost:5173"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,       # exact origins that may call the API
        allow_credentials=True,      # send cookies / auth headers cross-origin
        allow_methods=["*"],         # GET, POST, PUT, DELETE, OPTIONS, …
        allow_headers=["*"],         # Content-Type, Authorization, X-*, …
    )

    # ── Health ─────────────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ── Skills Registry ────────────────────────────────────────────────────────

    @app.get("/skills")
    def list_skills():
        return {"skills": registry.list_skills()}

    @app.get("/skills/{skill_id}")
    def get_skill(skill_id: str):
        try:
            skill = registry.get(skill_id)
            return skill.to_dict()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ── Run a Skill ────────────────────────────────────────────────────────────

    @app.post("/skills/{skill_id}/run")
    async def run_skill(skill_id: str, body: RunSkillRequest):
        try:
            result = await orchestrator.run_skill(skill_id, body.input, body.meta)
            return result
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ── Run a Pipeline ─────────────────────────────────────────────────────────

    @app.post("/pipeline/run")
    async def run_pipeline(body: RunPipelineRequest):
        if not body.skill_ids:
            raise HTTPException(status_code=400, detail="skill_ids must not be empty")
        result = await orchestrator.run_pipeline(body.skill_ids, body.input, body.meta)
        return result

    # ── Audit Log ──────────────────────────────────────────────────────────────

    @app.get("/audit")
    def get_audit(skill_id: str = None, event_type: str = None, since: str = None, limit: int = 100):
        entries = audit_log.query(skill_id=skill_id, event_type=event_type, since=since, limit=limit)
        return {"entries": entries, "count": len(entries)}

    @app.get("/audit/summary")
    def audit_summary():
        return audit_log.get_summary()

    # ── GitHub NL Agent ────────────────────────────────────────────────────────

    @app.post("/agent/github/run")
    async def run_github_agent(body: RunAgentRequest):
        if github_agent is None:
            raise HTTPException(status_code=503, detail="GitHub NL agent not configured — set GITHUB_TOKEN")
        try:
            return await github_agent.run(body.instruction, body.meta)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/agent/github/confirm")
    async def confirm_github_agent(body: ConfirmAgentRequest):
        if github_agent is None:
            raise HTTPException(status_code=503, detail="GitHub NL agent not configured — set GITHUB_TOKEN")
        return await github_agent.confirm(body.session_id, body.approve)

    # ── Jira NL Agent ──────────────────────────────────────────────────────────

    @app.post("/agent/jira/run")
    async def run_jira_agent(body: RunAgentRequest):
        if jira_agent is None:
            raise HTTPException(status_code=503, detail="Jira NL agent not configured — set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN")
        try:
            return await jira_agent.run(body.instruction, body.meta)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/agent/jira/confirm")
    async def confirm_jira_agent(body: ConfirmAgentRequest):
        if jira_agent is None:
            raise HTTPException(status_code=503, detail="Jira NL agent not configured — set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN")
        return await jira_agent.confirm(body.session_id, body.approve)

    # ── GitHub Webhook ─────────────────────────────────────────────────────────

    @app.post("/webhooks/github")
    async def github_webhook(request: Request, background_tasks: BackgroundTasks):
        event = request.headers.get("x-github-event")
        payload = await request.json()

        if event == "pull_request" and payload.get("action") in ("opened", "synchronize"):
            repo = payload["repository"]
            pr = payload["pull_request"]
            input_data = {
                "owner": repo["owner"]["login"],
                "repo": repo["name"],
                "pullNumber": pr["number"],
            }
            print(f"\n🔔 GitHub Webhook: PR #{pr['number']} {payload['action']}")
            background_tasks.add_task(
                orchestrator.run_pipeline,
                ["secrets-leak-prevention", "code-review", "documentation-sync"],
                input_data,
                {"triggered_by": "github-webhook", "event": event},
            )

        return {"received": True}

    return app
