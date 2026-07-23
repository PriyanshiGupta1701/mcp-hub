"""
prompts.py
----------
All prompt templates sent to Holmes, for every check type (Azure, Kubernetes,
SonarQube) and every stage (summarize, investigate, execute fix).
"""

SUMMARIZE_PROMPT_TEMPLATE = """A direct health check (no LLM involved) on Azure App Service '{app}' in
resource group '{rg}' just found signs of a possible problem. This evidence
was already gathered directly — you do not need to re-fetch it, though you
may use tools briefly for additional context if genuinely necessary:

App state: {app_state}
HTTP 5xx count in the last {window} minutes: {http_5xx_count}
Error lines found in application logs:
{log_excerpts}

The Azure subscription ID is '{subscription_id}' if you need it for any tool calls.

Based on this evidence, respond with ONLY a JSON object, no markdown, no extra
commentary, in exactly this shape:
{{"issue_found": true or false, "severity": "low" | "medium" | "high" | "critical", "category": "short-machine-readable-slug", "summary": "one sentence, under 200 characters", "details": "2-4 sentences describing the issue and root cause if evident"}}

If, on reflection, this evidence doesn't actually indicate a real problem
(e.g. a transient blip, expected behavior), return issue_found: false and
explain why in the summary."""

INVESTIGATE_PROMPT_TEMPLATE = """You previously found this issue with Azure App Service '{app}':
Category: {category}
Summary: {summary}
Details: {details}

Investigate the root cause by examining the code in the GitHub repository '{repo}'
(use your GitHub tools to browse and search the repo — look for the code path
that would produce this behavior).

Respond with ONLY a JSON object, no markdown, no extra commentary, in exactly this shape:
{{"fixable": true or false, "explanation": "1-3 sentences on the root cause, or why it's not fixable in code (e.g. pure infra/quota/config issue)", "proposed_change": "a concise, specific description of the exact code/config change to make — empty string if not fixable"}}"""

EXECUTE_FIX_PROMPT_TEMPLATE = """In the GitHub repository '{repo}', make this specific change:

{proposed_change}

(Context: this fixes — {explanation})

1. Create a new branch named 'holmes-autofix/{category}-{timestamp}' from the default branch.
2. Make the change as commit(s) on that branch.
3. Open a pull request from that branch into the default branch. Title it clearly
   referencing the issue. In the PR description include what was observed, the
   root cause, and exactly what was changed. State explicitly that this PR was
   opened automatically by Holmes and should be reviewed by a human before merging.
   Do NOT merge the PR yourself.

Respond with ONLY a JSON object, no markdown, no extra commentary, in exactly this shape:
{{"pr_opened": true or false, "pr_url": "url or empty string", "explanation": "1-3 sentences"}}"""


SUMMARIZE_PROMPT_TEMPLATE_K8S = """A direct health check (no LLM involved) on the Kubernetes deployment '{deployment}'
(namespace '{namespace}') just found signs of a possible problem. This evidence
was already gathered directly — you do not need to re-fetch it, though you
may use tools briefly for additional context if genuinely necessary:

Pod status summary:
{pod_summary}

Recent Warning events:
{events_summary}

Error lines / tracebacks found in pod logs:
{log_excerpts}

Based on this evidence, respond with ONLY a JSON object, no markdown, no extra
commentary, in exactly this shape:
{{"issue_found": true or false, "severity": "low" | "medium" | "high" | "critical", "category": "short-machine-readable-slug", "summary": "one sentence, under 200 characters", "details": "2-4 sentences describing the issue and root cause if evident"}}

If, on reflection, this evidence doesn't actually indicate a real problem
(e.g. a transient blip, expected behavior, deploy-in-progress), return
issue_found: false and explain why in the summary."""


# ── New prompt template (add alongside the other *_PROMPT_TEMPLATE consts) ─
# Unlike the Azure/K8s flow, we already know the concrete issues (file, line,
# rule, message) from SonarQube — no separate "investigate" step needed.
# This goes straight to proposing + executing a fix.
SONARQUBE_FIX_PROMPT_TEMPLATE = """A SonarQube scan of commit {commit_sha} (branch '{branch}') in the GitHub
repository '{repo}' found the following code quality issues:

{issues_text}

For each issue that has a clear, safe, minimal fix, make that fix. Skip any
issue that would require a significant design decision, is a false positive
in context, or that you're not confident about — do not guess.

1. Create a new branch named 'holmes-sonarqube-fix-{timestamp}' from the default branch.
2. Make the fixes as commit(s) on that branch. You may bundle multiple related
   fixes into one commit, or use separate commits — use your judgment for a
   clean, reviewable history.
3. Open a pull request from that branch into the default branch. Title it
   clearly (e.g. "Fix N SonarQube issues"). In the PR description, list each
   issue you fixed (file, line, rule) and each issue you deliberately skipped
   and why. State explicitly that this PR was opened automatically by Holmes
   in response to a SonarQube scan and should be reviewed by a human before
   merging. Do NOT merge the PR yourself.

If none of the issues have a safe, concrete fix, do NOT open a PR — just
explain why.

Respond with ONLY a JSON object, no markdown, no extra commentary, in exactly
this shape:
{{"pr_opened": true or false, "pr_url": "url or empty string", "issues_fixed": <int>, "issues_skipped": <int>, "explanation": "1-3 sentences"}}"""
