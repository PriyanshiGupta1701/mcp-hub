"""
Enterprise Skills Platform — Entry Point
Wires together: Registry → MCP Client → AI Engine → Orchestrator → FastAPI Server
"""

import os
import uvicorn
from dotenv import load_dotenv

from src.registry.skill_registry import SkillRegistry
from src.mcp.mcp_client import MCPClient
from src.mcp.adapters.github_mcp_adapter import GitHubMCPAdapter
from src.mcp.adapters.jira_adapter import JiraAdapter
from src.orchestration.ai_engine import AIEngine
from src.orchestration.orchestration_engine import OrchestrationEngine
from src.governance.audit_log import AuditLog
from src.agents.github_nl_agent import GitHubNLAgent
from src.agents.jira_nl_agent import JiraNLAgent
from src.skills.secrets_leak_prevention import SecretsLeakPreventionSkill
from src.skills.code_review import CodeReviewSkill
from src.skills.documentation_sync import DocumentationSyncSkill
from src.api.server import create_app

load_dotenv()


def build_app():
    print("🏗️  Starting Enterprise Skills Platform...\n")

    # 1. Skill Registry
    registry = SkillRegistry()
    registry.register(SecretsLeakPreventionSkill)
    registry.register(CodeReviewSkill)
    registry.register(DocumentationSyncSkill)

    # 2. MCP Client & Adapters
    mcp_client = MCPClient()

    github_mcp_adapter = None
    if token := os.getenv("GITHUB_TOKEN"):
        # Real MCP, not REST: connects to ghcr.io/github/github-mcp-server
        # over stdio, same as GitHubNLAgent below. Connection itself is opened
        # in the FastAPI lifespan handler (see create_app) since it needs an
        # event loop and should stay open for the app's lifetime, not be
        # reopened per-call.
        github_mcp_adapter = GitHubMCPAdapter(
            token=token,
            transport=os.getenv("GITHUB_MCP_TRANSPORT", "docker"),
            binary_path=os.getenv("GITHUB_MCP_BINARY_PATH"),
        )
        mcp_client.register_adapter("github", github_mcp_adapter)
    else:
        print("⚠️  GITHUB_TOKEN not set — GitHub adapter not loaded")

    if all([os.getenv("JIRA_BASE_URL"), os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")]):
        mcp_client.register_adapter("jira", JiraAdapter(
            base_url=os.environ["JIRA_BASE_URL"],
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
        ))
    else:
        print("⚠️  Jira credentials not set — Jira adapter not loaded")
    
    if raw := os.getenv("CORS_ORIGINS"):
        cors_origins = [o.strip() for o in raw.split(",")]

    # 3. AI Engine
    ai_engine = AIEngine(api_key=os.environ["GEMINI_API_KEY"])

    # 4. Governance
    audit_log = AuditLog()

    # 5. Orchestration Engine
    orchestrator = OrchestrationEngine(
        registry=registry,
        mcp_client=mcp_client,
        ai_engine=ai_engine,
        audit_log=audit_log,
    )

    # 6. GitHub NL Agent — separate from the deterministic skill pipeline above.
    # Talks to the *real* GitHub MCP server so Gemini can dynamically choose
    # which GitHub action to take from a plain-English instruction. Read-only
    # actions run immediately; write actions (merge/create/delete/etc.) pause
    # for confirm() before they touch GitHub. Requires Docker (or a local
    # github-mcp-server binary, via GITHUB_MCP_TRANSPORT=binary) to be available.
    github_agent = None
    if token := os.getenv("GITHUB_TOKEN"):
        github_agent = GitHubNLAgent(
            github_token=token,
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            audit_log=audit_log,
            mcp_transport=os.getenv("GITHUB_MCP_TRANSPORT", "docker"),
            mcp_binary_path=os.getenv("GITHUB_MCP_BINARY_PATH"),
            github_toolsets=os.getenv("GITHUB_MCP_TOOLSETS", "repos,issues,pull_requests"),
        )
    else:
        print("⚠️  GITHUB_TOKEN not set — GitHub NL agent not loaded")

    # 6.5 Jira NL Agent — same pattern as the GitHub NL agent above, but
    # talks to the Jira MCP server (sooperset/mcp-atlassian) so Gemini can
    # dynamically choose which Jira action to take from a plain-English
    # instruction. Read-only actions run immediately; write actions
    # (create issue, transition status, add comment, etc.) pause for
    # confirm() before they touch Jira. Requires Docker (or a local
    # mcp-atlassian binary, via JIRA_MCP_TRANSPORT=binary) to be available.
    jira_agent = None
    if all([os.getenv("JIRA_BASE_URL"), os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")]):
        jira_agent = JiraNLAgent(
            jira_base_url=os.environ["JIRA_BASE_URL"],
            jira_email=os.environ["JIRA_EMAIL"],
            jira_api_token=os.environ["JIRA_API_TOKEN"],
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            audit_log=audit_log,
            mcp_transport=os.getenv("JIRA_MCP_TRANSPORT", "docker"),
            mcp_binary_path=os.getenv("JIRA_MCP_BINARY_PATH"),
            confluence_base_url=os.getenv("CONFLUENCE_BASE_URL"),
        )
    else:
        print("⚠️  Jira credentials not set — Jira NL agent not loaded")

    # 7. FastAPI app
    app = create_app(
        registry=registry, orchestrator=orchestrator, audit_log=audit_log,
        github_agent=github_agent, jira_agent=jira_agent,
        github_mcp_adapter=github_mcp_adapter,
    )

    print("\nAvailable endpoints:")
    print("  GET  /health")
    print("  GET  /skills")
    print("  POST /skills/{skill_id}/run")
    print("  POST /pipeline/run")
    print("  GET  /audit")
    print("  POST /webhooks/github")
    print("  POST /agent/github/run")
    print("  POST /agent/github/confirm")
    print("  POST /agent/jira/run")
    print("  POST /agent/jira/confirm")
    print("\nRegistered skills:")
    for s in registry.list_skills():
        print(f"  [{s['id']}] {s['name']}")

    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
