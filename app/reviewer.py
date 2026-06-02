"""
reviewer.py
Sends PR context to OpenAI GPT-4 and gets a structured review decision.
"""

import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the AI architect and governance reviewer for the Creator Campaign OS project at Invictus Blue, a Malaysia-based media agency.

Your job is to review GitHub Pull Requests opened by Codex (an AI builder) and decide what to do with them.

THE PROJECT:
- Backend migration from Make.com to Python/FastAPI on Railway
- Airtable is the interface/database
- Google Sheets for KOL list input and client reports
- Apify for Instagram/TikTok scraping
- OpenAI for AI creator vetting
- Railway + FastAPI = new backend

SAFETY BOUNDARIES (NEVER approve PRs that touch these without flagging):
- Live Airtable schema changes (field/interface/button changes)
- Live Make scenario changes
- Railway env/secrets/settings changes
- GitHub secrets or API key changes
- OpenAI or Apify paid call logic changes
- Production data writebacks
- Switching live Airtable buttons from Make to backend
- Deleting or renaming fields/records
- Any destructive or irreversible action

YOUR OUTPUT must always be valid JSON in exactly this format:
{
  "decision": "MERGE" | "FIX" | "HOLD",
  "confidence": "high" | "medium" | "low",
  "summary": "One sentence summary of what this PR does",
  "reasoning": "2-3 sentences explaining your decision",
  "risks": ["risk 1", "risk 2"],
  "fix_instructions": "If decision is FIX, exact instructions for Codex. Otherwise empty string.",
  "human_approval_required": true | false,
  "human_approval_reason": "Why Cheuck needs to approve this. Empty string if not needed."
}

DECISION RULES:
- MERGE: PR is safe, tests pass, scope matches issue, no live system risk
- FIX: PR has problems Codex can fix (failing tests, wrong scope, missing files)
- HOLD: Human judgment needed — touches live systems, costs money, business logic change, risky

Always be conservative. When in doubt, HOLD."""


class AIReviewer:
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.daily_call_count = 0
        self.daily_limit = 50

    def review_pr(self, pr_details: dict) -> dict:
        """Send PR details to GPT-4 and get a structured review decision."""

        if self.daily_call_count >= self.daily_limit:
            logger.warning("Daily OpenAI call limit reached. Skipping review.")
            return {
                "decision": "HOLD",
                "confidence": "high",
                "summary": "Daily AI review limit reached.",
                "reasoning": "Control Tower has hit its daily OpenAI call cap. Cheuck needs to review manually.",
                "risks": ["Daily limit exceeded"],
                "fix_instructions": "",
                "human_approval_required": True,
                "human_approval_reason": "Daily OpenAI call limit reached. Manual review needed."
            }

        # Build the review prompt
        files_summary = ""
        for f in pr_details.get("files_changed", [])[:10]:  # cap at 10 files
            files_summary += f"\n- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})"
            if f.get("patch"):
                files_summary += f"\n```\n{f['patch'][:1500]}\n```"

        checks_summary = ""
        for c in pr_details.get("check_runs", []):
            status = c.get("conclusion") or c.get("status")
            checks_summary += f"\n- {c['name']}: {status}"

        prompt = f"""Review this Pull Request and return your decision as JSON.

PR #{pr_details.get('number')}: {pr_details.get('title')}
Branch: {pr_details.get('branch')} → {pr_details.get('base')}
Author: {pr_details.get('author')}
Draft: {pr_details.get('draft')}
Mergeable: {pr_details.get('mergeable')}

PR DESCRIPTION:
{pr_details.get('body', 'No description provided')}

FILES CHANGED:{files_summary}

CI CHECKS:{checks_summary if checks_summary else ' No checks found yet'}

LABELS: {', '.join(pr_details.get('labels', [])) or 'None'}

Return only valid JSON. No markdown, no explanation outside the JSON."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=1000
            )

            self.daily_call_count += 1
            raw = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            result = json.loads(raw)
            logger.info(f"Review complete for PR #{pr_details.get('number')}: {result.get('decision')}")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response as JSON: {e}")
            return self._fallback_hold("AI returned invalid JSON. Manual review needed.")
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return self._fallback_hold(f"OpenAI API error: {str(e)}")

    def reset_daily_count(self):
        self.daily_call_count = 0
        logger.info("Daily OpenAI call count reset.")

    def _fallback_hold(self, reason: str) -> dict:
        return {
            "decision": "HOLD",
            "confidence": "low",
            "summary": "Review failed.",
            "reasoning": reason,
            "risks": ["Automated review failed"],
            "fix_instructions": "",
            "human_approval_required": True,
            "human_approval_reason": reason
        }
