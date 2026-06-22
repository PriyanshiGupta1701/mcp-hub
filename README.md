# Mcp-Hub: MCP-Powered Enterprise Skills Platform

Automates engineering workflows using AI and MCP tool integrations.  
Built with **Python 3.12+**, **FastAPI**, and the **Google Gen AI SDK (Gemini)**.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Fill in GEMINI_API_KEY and GITHUB_TOKEN
# (the GitHub NL Agent also needs Docker running — see .env.example for the no-Docker alternative)

# 3. Run
python main.py
# or with auto-reload:
uvicorn main:app --reload
```

Server starts at `http://localhost:8000`  
Interactive API docs at `http://localhost:8000/docs`

---

## API Usage

### Run a skill manually
```bash
curl -X POST http://localhost:8000/skills/secrets-leak-prevention/run \
  -H "Content-Type: application/json" \
  -d '{"input": {"owner": "your-org", "repo": "your-repo", "pullNumber": 42}}'
```

### Run the full PR pipeline
```bash
curl -X POST http://localhost:8000/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "skill_ids": ["secrets-leak-prevention", "code-review", "documentation-sync"],
    "input": {"owner": "your-org", "repo": "your-repo", "pullNumber": 42}
  }'
```

### List skills
```bash
curl http://localhost:8000/skills
```

### View audit log
```bash
curl http://localhost:8000/audit
curl http://localhost:8000/audit/summary
```

### GitHub NL Agent — drive GitHub with plain English
Unlike the skills above (which call GitHub's REST API directly, in a fixed
sequence), this connects to the real **GitHub MCP server** and lets Gemini
decide which GitHub action to take. Read-only actions run immediately;
anything that mutates the repo (merge, create, delete, etc.) pauses and
waits for you to confirm it. **Requires Docker running** (or set
`GITHUB_MCP_TRANSPORT=binary` + `GITHUB_MCP_BINARY_PATH` in `.env` — see
`.env.example`).

```bash
# 1. Kick off a run
curl -X POST http://localhost:8000/agent/github/run \
  -H "Content-Type: application/json" \
  -d '{"instruction": "Merge PR #3 in your-org/your-repo if it has no conflicts"}'

# → {"status": "pending_confirmation", "session_id": "...", "proposed_action": {...}}

# 2. Approve (or decline) the proposed write action
curl -X POST http://localhost:8000/agent/github/confirm \
  -H "Content-Type: application/json" \
  -d '{"session_id": "<session_id from above>", "approve": true}'

# → {"status": "completed", "message": "..."}  (or another pending_confirmation,
#    if the instruction implied a follow-up write action, e.g. "create a branch
#    then open a PR from it")
```

---

## Project Structure

```
main.py                              # Entry point
src/
├── registry/skill_registry.py       # Skill store and validation
├── mcp/
│   ├── mcp_client.py                # Tool dispatcher
│   └── adapters/
│       ├── github_adapter.py        # GitHub REST API
│       └── jira_adapter.py          # Jira REST API
├── orchestration/
│   ├── ai_engine.py                 # Gemini (Google Gen AI SDK) wrapper
│   └── orchestration_engine.py      # Skill runner + pipeline
├── agents/
│   └── github_nl_agent.py           # NL-driven GitHub agent (real GitHub MCP server)
├── governance/audit_log.py          # Audit trail
├── skills/
│   ├── secrets_leak_prevention.py
│   ├── code_review.py
│   └── documentation_sync.py
└── api/server.py                    # FastAPI app + webhook
```

---

## Adding a New Skill

Create `src/skills/your_skill.py`:

```python
from src.registry.skill_registry import SkillDefinition

SYSTEM_PROMPT = "You are... Respond ONLY with valid JSON: {...}"

async def _execute(ctx) -> dict:
    ctx.log("Step description...")
    data = await ctx.mcp_client.call("github", "getPRDiff", {**ctx.input})
    analysis = await ctx.ai_engine.analyze(SYSTEM_PROMPT, str(data))
    return {"result": analysis["result"]}

YourSkill = SkillDefinition(
    id="your-skill",
    name="Your Skill",
    description="What it does",
    category="security",
    required_tools=["github"],
    execute=_execute,
)
```

Register in `main.py`:
```python
from src.skills.your_skill import YourSkill
registry.register(YourSkill)
```

---

## GitHub Webhook

1. Repo → Settings → Webhooks → Add webhook
2. Payload URL: `https://your-server.com/webhooks/github`
3. Content type: `application/json`
4. Events: **Pull requests**

Every PR open/update triggers the full pipeline automatically.

---

## Roadmap

- [ ] PostgreSQL persistence for audit log
- [ ] Auth middleware (API keys / OAuth)
- [ ] Release Validation Skill
- [ ] Release Risk Predictor Skill
- [ ] Enterprise Cost Leak Detector (AWS/Azure)
- [ ] Software License Utilization Skill
- [ ] React monitoring dashboard
