"""
planner.py
Project-level intelligence for Control Tower V2.
Knows the full Creator Campaign OS roadmap.
Answers "what's next?" in plain English.
Creates GitHub Issues for the next milestone.
"""

import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── PROJECT MEMORY ──
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

MIGRATION STATUS — COMPLETED:
- Backend exists, deployed on Railway
- GitHub PR/check workflow running
- Governance checks active
- /schema-check upgraded to real Airtable metadata inspection
- Repo documentation cleaned up
- Creator import dry run working
- Batch read, campaign read, sheet read endpoints working
- Scraping plan dry run working
- Sandbox creator import test working

MIGRATION STATUS — NOT YET DONE:
- Scenario 2 (KOL import) not yet replaced by backend in live
- Scenario 3A (scraping) not yet replaced
- Scenario 3B (AI vetting) not yet replaced
- Scenario 4 (client report) not yet replaced
- Live Airtable buttons not switched
- Make not retired

NEXT MILESTONE: Scenario 2 Backend Parity
Replace the KOL list import step with backend in sandbox.
Backend should: read campaign batch, read campaign brief, read Google Sheet,
parse creator rows, create Creator Vetting records in sandbox, match Make Scenario 2 behavior.
This is the first real piece of Make being replaced.

ROADMAP AFTER THAT (in order):
1. Scenario 2 live switch (after sandbox parity proven)
2. Scenario 3A: Creator scraping migration (Apify calls from backend)
3. Scenario 3B: AI vetting migration (OpenAI creator evaluation from backend)
4. Scenario 4: Client report generation migration
5. Full sandbox end-to-end rehearsal (backend vs Make output comparison)
6. Controlled live switch decision (approval required)
7. Expand to full Creator Campaign OS vision

END GOAL:
Campaign team uploads brief + creator list.
System structures work, recommends creators, explains logic, manages workflow,
prepares client outputs, stores learnings.
Humans stay in control of judgment and approval.
Make is fully retired.

COST BENCHMARKS:
Normal 8-creator batch: RM25-35 variable cost
Normal 15-creator batch: RM45-60 variable cost
Alert threshold: any single batch above RM100
Monthly alert: above RM800/month incremental cost
Critical monthly: above RM1,200/month

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
- Any change that degrades OS quality or team experience
"""

PLANNER_SYSTEM_PROMPT = """You are the project intelligence layer for the Creator Campaign OS Control Tower.

You know the full project context, migration status, and roadmap.
You speak to Cheuck, the Deputy GM, in plain English — like a smart project lead giving a briefing.
You never use issue numbers, file names, or technical jargon unless asked.
You always explain where the project stands and what matters next in business terms.

When asked "what's next?", you:
1. Briefly say what was recently completed (1 sentence)
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


class ProjectPlanner:
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def whats_next(self, open_issues: list, open_prs: list) -> str:
        """Return plain English project status and next step recommendation."""

        issues_summary = ""
        if open_issues:
            issues_summary = "\nCurrently open GitHub Issues:\n" + "\n".join([
                f"- #{i.number}: {i.title}" for i in open_issues[:5]
            ])

        prs_summary = ""
        if open_prs:
            prs_summary = "\nCurrently open PRs:\n" + "\n".join([
                f"- PR #{p.number}: {p.title}" for p in open_prs[:5]
            ])

        prompt = f"""Based on the project context, give Cheuck a plain English briefing.

{issues_summary}
{prs_summary}

Tell him:
1. What was recently completed (1 sentence, based on migration status)
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

    def create_next_issue_brief(self, milestone_hint: str = "") -> dict:
        """Generate a structured GitHub Issue brief for the next milestone."""

        prompt = f"""Create a detailed GitHub Issue brief for the next milestone in the Creator Campaign OS migration.

{"The user indicated: " + milestone_hint if milestone_hint else "Use the roadmap to determine the next milestone."}

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

    def generate_weekly_summary(self, merged_prs: list, open_issues: list) -> str:
        """Generate a Monday morning weekly summary."""

        merged_summary = ""
        if merged_prs:
            merged_summary = "Recently merged PRs:\n" + "\n".join([
                f"- {pr}" for pr in merged_prs[:10]
            ])

        prompt = f"""Generate a Monday morning weekly summary for Cheuck.

{merged_summary}

Open issues: {len(open_issues)}

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
