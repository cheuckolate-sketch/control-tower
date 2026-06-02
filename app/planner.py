"""
planner.py
Project-level intelligence for Control Tower V2.
Option B: Reads merged PRs dynamically from GitHub before every response.
Answers "what's next?" based on actual progress, not hardcoded context.
"""

import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the project intelligence layer for the Creator Campaign OS Control Tower.

You know the full project context, migration status, and roadmap.
You speak to Cheuck, the Deputy GM, in plain English — like a smart project lead giving a briefing.
You never use issue numbers, file names, or technical jargon unless asked.
You always explain where the project stands and what matters next in business terms.

When asked "what's next?", you:
1. Briefly say what was recently completed (1 sentence, based on actual merged PRs)
2. Explain what the next milestone is and why it matters (2-3 sentences)
3. Ask if Cheuck wants you to kick it off

When asked to create a GitHub Issue for the next milestone, you output a structured JSON with:
{
  "title": "Issue title (technical, for GitHub)",
  "body": "Full issue brief (detailed, for the AI builder to follow)",
  "milestone": "Plain English name of this milestone"
}

Keep issue bodies detailed enough that an AI builder can implement without asking questions.
Include: goal, success criteria, scope, safety rules, expected files changed, test requirements.
"""

PROJECT_CONTEXT = """
PRODUCT: Creator Campaign OS
COMPANY: Invictus Blue, a Malaysia-based media agency

WHAT IT IS:
A workflow and intelligence system for managing creator/KOL campaigns end to end.
It helps campaign teams move from manual checking, scattered spreadsheets, and fragile automation
to structured creator intelligence, guided workflow, AI-assisted evaluation, and client-ready outputs.

CURRENT ACTIVE MODULE: IB Creator Review & Intelligence Hub V2

TECH STACK:
- Airtable: interface + campaign/creator data
- Make: current automation layer (fallback, NOT being removed yet)
- Google Sheets: KOL list input + client report output
- Apify: Instagram/TikTok scraping
- OpenAI: AI creator vetting/ranking/rationale
- Python/FastAPI on Railway: new backend (replacing Make step by step)
- GitHub: source of truth for backend development

ROADMAP (in order):
Phase 1: Backend Foundation
Phase 2: Scenario 2 — KOL Import (replace Make import with backend)
Phase 3: Scenario 3A — Creator Scraping (Apify calls from backend)
Phase 4: Scenario 3B — AI Vetting (OpenAI creator evaluation from backend)
Phase 5: Scenario 4 — Client Report generation
Phase 6: End-to-End Sandbox Rehearsal
Phase 7: Live Switch Decision (approval required)

SAFETY RULES (NEVER auto-approve):
- Live Airtable changes
- Make scenario changes
- Railway env/secrets changes
- OpenAI or Apify paid call logic changes
- Production data writebacks
- Switching live buttons from Make to backend
- Any cost increase
- Any change to client-facing outputs
- Any change to creator ranking/scoring/rationale logic

COST BENCHMARKS:
Normal 8-creator batch: RM25-35
Normal 15-creator batch: RM45-60
Alert threshold: any single batch above RM100
Monthly alert: above RM800/month
Critical: above RM1,200/month
"""


class ProjectPlanner:
    def __init__(self, api_key: str, github_client, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.github = github_client
        self.model = model

    # ─────────────────────────────────────────────
    # DYNAMIC PR FETCHING
    # ─────────────────────────────────────────────

    def _get_merged_prs(self, limit: int = 20) -> list[dict]:
        """
        Fetch recently merged PRs from creator-campaign-os-backend.
        For each PR, also fetches the diff so GPT-4o knows what was actually built.
        Returns list of dicts with number, title, body, diff.
        """
        try:
            import requests
            import os
            token = os.environ["GITHUB_TOKEN"]
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            }
            url = "https://api.github.com/repos/cheuckolate-sketch/creator-campaign-os-backend/pulls"
            params = {"state": "closed", "per_page": limit, "sort": "updated", "direction": "desc"}
            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
            prs = resp.json()

            merged = []
            for pr in prs:
                if not pr.get("merged_at"):
                    continue

                # Fetch the diff for this PR
                diff_text = ""
                try:
                    diff_resp = requests.get(
                        pr["url"],
                        headers={**headers, "Accept": "application/vnd.github.diff"},
                    )
                    if diff_resp.status_code == 200:
                        # Cap diff at 1500 chars per PR to stay within context limits
                        diff_text = diff_resp.text[:1500]
                except Exception as diff_err:
                    logger.warning(f"Could not fetch diff for PR #{pr['number']}: {diff_err}")

                merged.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "body": (pr.get("body") or "")[:300],
                    "merged_at": pr.get("merged_at"),
                    "diff": diff_text,
                })

            logger.info(f"Fetched {len(merged)} merged PRs with diffs from GitHub.")
            return merged
        except Exception as e:
            logger.error(f"Failed to fetch merged PRs: {e}")
            return []

    def _get_phases(self) -> str:
        """
        Read phases.json from creator-campaign-os-backend.
        Returns formatted string for GPT-4o context, or empty string if not found.
        """
        try:
            import requests
            import os
            import base64
            token = os.environ["GITHUB_TOKEN"]
            url = "https://api.github.com/repos/cheuckolate-sketch/creator-campaign-os-backend/contents/docs/phases.json"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            }
            resp = requests.get(url, headers=headers)
            if resp.status_code == 404:
                return ""
            resp.raise_for_status()
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            phases = json.loads(content)

            # Format phases into readable summary for GPT-4o
            lines = ["PHASE MAP (from phases.json):"]
            for phase in phases.get("phases", []):
                icon = {"complete": "✅", "in_progress": "🔄", "not_started": "⏳"}.get(phase["status"], "❓")
                lines.append(f"{icon} Phase {phase['id']}: {phase['name']} — {phase['status']}")
                if phase.get("deliverables"):
                    lines.append(f"   Remaining: {', '.join(phase['deliverables'][:5])}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to read phases.json: {e}")
            return ""

    def _format_pr_summary(self, merged_prs: list[dict]) -> str:
        if not merged_prs:
            return "No merged PRs found."
        lines = []
        for pr in merged_prs:
            body_snippet = pr["body"].replace("\n", " ").strip()
            diff_snippet = pr.get("diff", "").strip()
            entry = f"PR #{pr['number']}: {pr['title']} — {body_snippet}"
            if diff_snippet:
                entry += f"\n  [Diff preview]: {diff_snippet[:400]}"
            lines.append(entry)
        return "\n".join(lines)

    # ─────────────────────────────────────────────
    # PUBLIC INTERFACE
    # ─────────────────────────────────────────────

    def whats_next(self, open_issues: list = None, open_prs: list = None, merged_prs: list[dict] = None) -> str:
        """
        Return plain English project status and next step recommendation.
        Now reads merged PRs dynamically if not passed in.
        """
        if merged_prs is None:
            merged_prs = self._get_merged_prs(limit=20)

        pr_summary = self._format_pr_summary(merged_prs)
        phases_context = self._get_phases()

        issues_summary = ""
        if open_issues:
            issues_summary = "\nCurrently open GitHub Issues:\n" + "\n".join([
                f"- #{i.number}: {i.title}" for i in (open_issues or [])[:5]
            ])

        prs_summary = ""
        if open_prs:
            prs_summary = "\nCurrently open PRs:\n" + "\n".join([
                f"- PR #{p.number}: {p.title}" for p in (open_prs or [])[:5]
            ])

        prompt = f"""Based on the actual merged PRs and phase map below, give Cheuck a plain English briefing.

{phases_context}

MERGED PRs (actual progress, including diffs):
{pr_summary}

{issues_summary}
{prs_summary}

Tell him:
1. What was recently completed (1 sentence, based on actual merged PRs and diffs — not the static roadmap)
2. What the next milestone is and why it matters (2-3 sentences, business language)
3. Ask if he wants you to kick it off

Keep it conversational. No bullet points. No technical jargon. Max 5 sentences total."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT + "\n\nPROJECT CONTEXT:\n" + PROJECT_CONTEXT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=300
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Planner error: {e}")
            return "Having trouble checking project status right now. Try again in a moment."

    def create_next_issue_brief(self, milestone_hint: str = "", merged_prs: list[dict] = None) -> dict:
        """Generate a structured GitHub Issue brief for the next milestone."""

        if merged_prs is None:
            merged_prs = self._get_merged_prs(limit=20)

        pr_summary = self._format_pr_summary(merged_prs)
        phases_context = self._get_phases()

        prompt = f"""Based on the actual merged PRs and phase map below, determine the next milestone and create a detailed GitHub Issue brief.

{phases_context}

MERGED PRs (actual progress, including diffs):
{pr_summary}

{"The user indicated: " + milestone_hint if milestone_hint else "Use the merged PRs and roadmap to determine the next milestone automatically."}

Return valid JSON only. No markdown fences. Format:
{{
  "title": "issue title",
  "body": "full detailed issue brief",
  "milestone": "plain English milestone name"
}}

The body must include:
- Goal
- Success criteria
- Scope (what is allowed, what is not)
- Safety classification
- Expected files changed
- Test requirements
- Rollback plan

Make it detailed enough for an AI builder to implement without asking questions."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT + "\n\nPROJECT CONTEXT:\n" + PROJECT_CONTEXT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=2000
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            logger.error(f"Issue creation error: {e}")
            return None

    def generate_weekly_summary(self, merged_prs: list[dict] = None, open_issues: list = None) -> str:
        """Generate a Monday morning weekly summary based on actual merged PRs."""

        if merged_prs is None:
            merged_prs = self._get_merged_prs(limit=30)

        pr_summary = self._format_pr_summary(merged_prs)
        phases_context = self._get_phases()

        prompt = f"""Generate a Monday morning weekly summary for Cheuck based on actual merged PRs and phase map.

{phases_context}

MERGED PRs (actual progress, including diffs):
{pr_summary}

Open issues: {len(open_issues) if open_issues else 0}

Tell him:
1. What got done recently (plain English, no PR numbers)
2. Where the project stands overall (one sentence)
3. What's coming next this week
4. Any flags or things to watch

Keep it brief. Conversational. Business language only. Max 6 sentences."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT + "\n\nPROJECT CONTEXT:\n" + PROJECT_CONTEXT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=300
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Weekly summary error: {e}")
            return "Could not generate weekly summary."
