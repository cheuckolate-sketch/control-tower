"""
project_manager.py
Control Tower V3 — unified project intelligence.
Replaces planner.py and phase_manager.py entirely.

Knows the project inside out:
- Reads phases.json from GitHub (generates on first run)
- Reads merged PRs with diffs
- Tracks velocity and estimates completion
- Detects stalls
- Gap analysis: expected deliverables vs what's actually merged
- Creates GitHub Issues for the builder
- Manages phase transitions
- Generates briefings, weekly summaries
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from openai import OpenAI

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PRODUCT_REPO = "cheuckolate-sketch/creator-campaign-os-backend"
PHASES_PATH = "docs/phases.json"

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

PROJECT_CONTEXT = """
PRODUCT: Creator Campaign OS
COMPANY: Invictus Blue, a Malaysia-based media agency

WHAT IT IS:
A workflow and intelligence system for managing creator/KOL campaigns end to end.
Helps campaign teams move from manual spreadsheets and fragile automation to structured
creator intelligence, guided workflow, AI-assisted evaluation, and client-ready outputs.

TECH STACK:
- Airtable: interface + campaign/creator data
- Make: current automation layer (being replaced step by step)
- Google Sheets: KOL list input + client report output
- Apify: Instagram/TikTok scraping
- OpenAI: AI creator vetting/ranking/rationale
- Python/FastAPI on Railway: new backend
- GitHub: source of truth for backend development

ROADMAP:
Phase 1: Backend Foundation — core infra, DB schema, API scaffolding
Phase 2: Scenario 2 — KOL Import (replace Make import with backend in sandbox)
Phase 3: Scenario 3A — Creator Scraping (Apify calls from backend)
Phase 4: Scenario 3B — AI Vetting (OpenAI creator evaluation from backend)
Phase 5: Scenario 4 — Client Report generation
Phase 6: End-to-End Sandbox Rehearsal (full dry run)
Phase 7: Live Switch Decision (go/no-go, Cheuck approval required)

SAFETY RULES — NEVER AUTO-APPROVE:
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

PM_SYSTEM_PROMPT = """You are the project manager for the Creator Campaign OS build at Invictus Blue.
You speak directly to Cheuck, the Deputy GM who commissioned this system.

Your job is to know this project inside out and manage it proactively.
You speak in plain English. No jargon, no PR numbers, no file names unless specifically asked.
You think in business terms: what's done, what matters next, what could go wrong.

You are not a chatbot. You are a project manager.
"""


class ProjectManager:
    def __init__(
        self,
        github_client=None,
        ai_briefings_enabled: bool = True,
        intent_parser_enabled: bool = False,
        cache_ttl_seconds: int = 900,
        call_recorder=None,
    ):
        self.openai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
        self.github = github_client
        self._phase_map_cache = None
        self.ai_briefings_enabled = ai_briefings_enabled
        self.intent_parser_enabled = intent_parser_enabled
        self.cache_ttl_seconds = cache_ttl_seconds
        self.call_recorder = call_recorder
        self._briefing_cache: dict[str, tuple[float, str]] = {}

    def _record_openai_call(self, category: str):
        if self.call_recorder:
            self.call_recorder(category)

    def _cached(self, key: str) -> str | None:
        cached = self._briefing_cache.get(key)
        if not cached:
            return None
        saved_at, value = cached
        if time.time() - saved_at <= self.cache_ttl_seconds:
            return value
        self._briefing_cache.pop(key, None)
        return None

    def _store_cache(self, key: str, value: str) -> str:
        self._briefing_cache[key] = (time.time(), value)
        return value

    def _create_chat_completion(self, category: str, **kwargs):
        if not self.openai:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        response = self.openai.chat.completions.create(**kwargs)
        self._record_openai_call(category)
        return response

    def get_phase_map_snapshot(self) -> dict | None:
        if self._phase_map_cache:
            return self._phase_map_cache
        existing = self._load_phases()
        if existing:
            self._phase_map_cache = existing
        return existing

    def get_active_phase_snapshot(self) -> dict | None:
        phase_map = self.get_phase_map_snapshot()
        if not phase_map:
            return None
        return self.get_active_phase(phase_map)

    def _deterministic_checkpoint_summary(self, command_name: str) -> str:
        phase_map = self.get_phase_map_snapshot()
        active = self.get_active_phase(phase_map) if phase_map else None
        active_text = "Not verified"
        if active:
            active_text = f"Phase {active.get('id')}: {active.get('name')} [{active.get('status')}]"
        merged = self._get_merged_prs(limit=3)
        issues = self._get_open_issues()
        merged_text = "\n".join([f"- PR #{pr['number']}: {pr['title']}" for pr in merged[:3]]) or "- Not verified"
        issue_text = "\n".join([f"- Issue #{issue['number']}: {issue['title']}" for issue in issues[:3]]) or "- None"
        return (
            f"*{command_name} checkpoint summary*\n\n"
            f"*Active phase:* {active_text}\n"
            f"*Latest merged PRs:*\n{merged_text}\n\n"
            f"*Open issues:*\n{issue_text}\n\n"
            "Deployment status: Not verified.\n"
            "Live runtime state: Not verified.\n"
            "Use `checkpoint <runtime fact>` after checking the live endpoint or Railway deploy state."
        )

    # ─────────────────────────────────────────────
    # CORE DATA FETCHING
    # ─────────────────────────────────────────────

    def _get_merged_prs(self, limit: int = 30) -> list[dict]:
        """Fetch merged PRs with diffs from the product repo."""
        try:
            url = f"https://api.github.com/repos/{PRODUCT_REPO}/pulls"
            params = {"state": "closed", "per_page": limit, "sort": "updated", "direction": "desc"}
            resp = requests.get(url, headers=GITHUB_HEADERS, params=params)
            resp.raise_for_status()

            merged = []
            for pr in resp.json():
                if not pr.get("merged_at"):
                    continue
                diff_text = ""
                try:
                    diff_resp = requests.get(
                        pr["url"],
                        headers={**GITHUB_HEADERS, "Accept": "application/vnd.github.diff"},
                    )
                    if diff_resp.status_code == 200:
                        diff_text = diff_resp.text[:1500]
                except Exception:
                    pass

                merged.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "body": (pr.get("body") or "")[:300],
                    "merged_at": pr.get("merged_at"),
                    "diff": diff_text,
                })

            logger.info(f"Fetched {len(merged)} merged PRs.")
            return merged
        except Exception as e:
            logger.error(f"Failed to fetch merged PRs: {e}")
            return []

    def _get_open_issues(self) -> list[dict]:
        """Fetch open issues from product repo."""
        try:
            url = f"https://api.github.com/repos/{PRODUCT_REPO}/issues"
            params = {"state": "open", "per_page": 30}
            resp = requests.get(url, headers=GITHUB_HEADERS, params=params)
            resp.raise_for_status()
            # Filter out PRs (GitHub issues endpoint returns PRs too)
            return [i for i in resp.json() if "pull_request" not in i]
        except Exception as e:
            logger.error(f"Failed to fetch open issues: {e}")
            return []

    def _load_phases(self) -> dict | None:
        """Read phases.json from the product repo."""
        try:
            url = f"https://api.github.com/repos/{PRODUCT_REPO}/contents/{PHASES_PATH}"
            resp = requests.get(url, headers=GITHUB_HEADERS)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to load phases.json: {e}")
            return None

    def _save_phases(self, phase_map: dict) -> None:
        """Write phases.json to the product repo."""
        url = f"https://api.github.com/repos/{PRODUCT_REPO}/contents/{PHASES_PATH}"
        content = base64.b64encode(
            json.dumps(phase_map, indent=2).encode("utf-8")
        ).decode("utf-8")

        sha = None
        resp = requests.get(url, headers=GITHUB_HEADERS)
        if resp.status_code == 200:
            sha = resp.json().get("sha")

        payload = {
            "message": f"chore: update phases.json [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC]",
            "content": content,
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, headers=GITHUB_HEADERS, json=payload)
        resp.raise_for_status()
        self._phase_map_cache = phase_map
        logger.info("phases.json saved.")

    def _format_pr_context(self, merged_prs: list[dict]) -> str:
        if not merged_prs:
            return "No merged PRs yet."
        lines = []
        for pr in merged_prs:
            body = pr["body"].replace("\n", " ").strip()
            diff = pr.get("diff", "").strip()
            entry = f"PR #{pr['number']} (merged {pr['merged_at'][:10]}): {pr['title']}"
            if body:
                entry += f"\n  Description: {body}"
            if diff:
                entry += f"\n  Code changes: {diff[:500]}"
            lines.append(entry)
        return "\n\n".join(lines)

    def _format_phases_context(self, phase_map: dict) -> str:
        if not phase_map:
            return "Phase map not yet generated."
        lines = ["PHASE MAP:"]
        for phase in phase_map.get("phases", []):
            icon = {"complete": "✅", "in_progress": "🔄", "not_started": "⏳"}.get(phase["status"], "❓")
            lines.append(f"{icon} Phase {phase['id']}: {phase['name']} [{phase['status']}]")
            if phase.get("deliverables"):
                lines.append(f"   Remaining deliverables: {', '.join(phase['deliverables'][:5])}")
        return "\n".join(lines)

    # ─────────────────────────────────────────────
    # VELOCITY + STALL DETECTION
    # ─────────────────────────────────────────────

    def _calculate_velocity(self, merged_prs: list[dict]) -> dict:
        """
        Calculate merge velocity and detect stalls.
        Returns: prs_per_day, last_merge_at, days_since_last_merge, is_stalled
        """
        if not merged_prs:
            return {"prs_per_day": 0, "last_merge_at": None, "days_since_last_merge": None, "is_stalled": False}

        now = datetime.now(timezone.utc)

        # Parse merge timestamps
        timestamps = []
        for pr in merged_prs:
            try:
                ts = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
                timestamps.append(ts)
            except Exception:
                continue

        if not timestamps:
            return {"prs_per_day": 0, "last_merge_at": None, "days_since_last_merge": None, "is_stalled": False}

        timestamps.sort(reverse=True)
        last_merge = timestamps[0]
        days_since = (now - last_merge).total_seconds() / 86400

        # Velocity over last 7 days
        week_ago = now - timedelta(days=7)
        recent = [t for t in timestamps if t > week_ago]
        prs_per_day = round(len(recent) / 7, 1)

        return {
            "prs_per_day": prs_per_day,
            "last_merge_at": last_merge.strftime("%Y-%m-%d %H:%M UTC"),
            "days_since_last_merge": round(days_since, 1),
            "is_stalled": days_since >= 2,
        }

    def check_for_stall(self) -> str | None:
        """
        Returns a stall alert message if no PRs merged in 48hrs.
        Returns None if everything is moving.
        """
        merged_prs = self._get_merged_prs(limit=10)
        velocity = self._calculate_velocity(merged_prs)
        if velocity["is_stalled"]:
            days = velocity["days_since_last_merge"]
            last = velocity["last_merge_at"]
            return (
                f"⚠️ *Build stall detected*\n\n"
                f"No PRs merged in {days} days. Last merge: {last}.\n\n"
                f"Builder may be stuck. Worth a look."
            )
        return None

    # ─────────────────────────────────────────────
    # PHASE MANAGEMENT
    # ─────────────────────────────────────────────

    def get_or_init_phases(self) -> dict:
        """Load phase map or generate it on first run."""
        if self._phase_map_cache:
            return self._phase_map_cache

        existing = self._load_phases()
        if existing:
            self._phase_map_cache = existing
            return existing

        logger.info("No phase map found. Generating via OpenAI...")
        merged_prs = self._get_merged_prs(limit=30)
        phase_map = self._generate_phase_map(merged_prs)
        self._save_phases(phase_map)
        return phase_map

    def _generate_phase_map(self, merged_prs: list[dict]) -> dict:
        pr_context = self._format_pr_context(merged_prs)
        prompt = f"""Based on the merged PRs below, generate the phase map for this project.
Determine which phases are complete, which is in progress, and which haven't started.
For each in-progress or not-started phase, list the key remaining deliverables.

{pr_context}

Respond with JSON only:
{{
  "generated_at": "ISO timestamp",
  "last_updated": "ISO timestamp",
  "phases": [
    {{
      "id": 1,
      "name": "Phase name",
      "description": "What this phase covers",
      "status": "complete" | "in_progress" | "not_started",
      "deliverables": ["remaining tasks only"],
      "started_at": "ISO timestamp or null",
      "completed_at": "ISO timestamp or null"
    }}
  ]
}}"""

        response = self._create_chat_completion(
            "background_ai",
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PM_SYSTEM_PROMPT + "\n\n" + PROJECT_CONTEXT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
        )
        phase_map = json.loads(response.choices[0].message.content)
        now = datetime.now(timezone.utc).isoformat()
        phase_map.setdefault("generated_at", now)
        phase_map["last_updated"] = now
        return phase_map

    def get_active_phase(self, phase_map: dict) -> dict | None:
        for phase in phase_map.get("phases", []):
            if phase["status"] in ("in_progress", "not_started"):
                return phase
        return None

    def approve_phase(self, phase_id: int) -> str:
        """Mark a phase complete and advance to the next. Returns confirmation message."""
        phase_map = self.get_or_init_phases()
        now = datetime.now(timezone.utc).isoformat()

        advanced = False
        next_phase_name = None

        for phase in phase_map["phases"]:
            if phase["id"] == phase_id:
                phase["status"] = "complete"
                phase["completed_at"] = now
            elif phase["id"] == phase_id + 1:
                phase["status"] = "in_progress"
                phase["started_at"] = now
                next_phase_name = phase["name"]
                advanced = True

        phase_map["last_updated"] = now
        self._save_phases(phase_map)
        self._phase_map_cache = phase_map

        if advanced:
            return (
                f"✅ *Phase {phase_id} approved and marked complete.*\n\n"
                f"Starting Phase {phase_id + 1}: {next_phase_name}\n\n"
                f"Send `what's next` to get the first task briefing."
            )
        else:
            return f"✅ Phase {phase_id} marked complete. That was the final phase — project is done."

    def check_phase_completion(self) -> tuple[bool, dict | None, str]:
        """
        Check if the active phase looks complete based on merged PRs.
        Returns (is_complete, active_phase, summary_message)
        """
        phase_map = self.get_or_init_phases()
        active = self.get_active_phase(phase_map)
        if not active or active["status"] == "not_started":
            return False, active, ""

        merged_prs = self._get_merged_prs(limit=30)
        pr_context = self._format_pr_context(merged_prs)

        prompt = f"""Evaluate whether this project phase is complete based on merged PRs.

Phase: {active['name']}
Description: {active['description']}
Expected deliverables: {json.dumps(active.get('deliverables', []))}

Merged PRs:
{pr_context}

Respond with JSON only:
{{
  "complete": true | false,
  "confidence": "high" | "medium" | "low",
  "done": ["deliverables confirmed complete"],
  "missing": ["deliverables still missing"],
  "summary": "one sentence for Cheuck"
}}"""

        response = self._create_chat_completion(
            "background_ai",
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=600,
        )
        result = json.loads(response.choices[0].message.content)
        is_complete = result.get("complete") and result.get("confidence") in ("high", "medium")

        if is_complete:
            msg = (
                f"🏁 *Phase {active['id']} looks complete*\n\n"
                f"{result.get('summary', '')}\n\n"
                f"Reply `approved` to move to the next phase."
            )
        else:
            missing = result.get("missing", [])
            msg = ""
            if missing:
                msg = f"Still in progress. Missing: {', '.join(missing[:3])}"

        return is_complete, active, msg

    # ─────────────────────────────────────────────
    # BRIEFINGS
    # ─────────────────────────────────────────────

    def get_full_briefing(self) -> str:
        """What's next — full context briefing for Cheuck."""
        if not self.ai_briefings_enabled:
            return self._deterministic_checkpoint_summary("What's next")

        merged_prs = self._get_merged_prs(limit=30)
        phase_map = self.get_or_init_phases()
        open_issues = self._get_open_issues()
        velocity = self._calculate_velocity(merged_prs)

        pr_context = self._format_pr_context(merged_prs)
        phases_context = self._format_phases_context(phase_map)

        velocity_note = ""
        if velocity["prs_per_day"] > 0:
            velocity_note = f"Current velocity: {velocity['prs_per_day']} PRs/day. Last merge: {velocity['last_merge_at']}."
        if velocity["is_stalled"]:
            velocity_note = f"⚠️ No merges in {velocity['days_since_last_merge']} days. Build may be stalled."

        open_issues_note = f"Open issues in backlog: {len(open_issues)}" if open_issues else ""
        cache_key = f"full_briefing:{phase_map.get('last_updated', '')}:{len(merged_prs)}:{len(open_issues)}:{velocity.get('last_merge_at')}"
        cached = self._cached(cache_key)
        if cached:
            return cached

        prompt = f"""Give Cheuck a plain English project briefing. Max 5 sentences.

{phases_context}

RECENT MERGED PRs (what was actually built):
{pr_context}

{velocity_note}
{open_issues_note}

Tell him:
1. What was recently completed (based on actual PRs, not assumptions)
2. What the next milestone is and why it matters
3. Whether Cheuck needs to do anything manually before the next milestone
4. Whether credentials/API setup, cost, live-system impact, or client-output risk is likely
5. The exact next Telegram command to type
6. What not to do yet

Plain English. No PR numbers. No file names. No jargon. Conversational."""

        response = self._create_chat_completion(
            "pm_briefing",
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PM_SYSTEM_PROMPT + "\n\n" + PROJECT_CONTEXT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=350,
        )
        return self._store_cache(cache_key, response.choices[0].message.content.strip())

    def get_phase_summary(self) -> str:
        """All phases + status formatted for Telegram."""
        phase_map = self.get_or_init_phases()
        lines = ["*Project Phases*\n"]
        for phase in phase_map.get("phases", []):
            icon = {"complete": "✅", "in_progress": "🔄", "not_started": "⏳"}.get(phase["status"], "❓")
            lines.append(f"{icon} Phase {phase['id']}: {phase['name']}")
            if phase["status"] == "in_progress" and phase.get("deliverables"):
                remaining = phase["deliverables"][:3]
                lines.append(f"   ↳ Remaining: {', '.join(remaining)}")
        return "\n".join(lines)

    def get_active_phase_detail(self) -> str:
        """Where are we — current phase detail with gap analysis."""
        if not self.ai_briefings_enabled:
            return self._deterministic_checkpoint_summary("Where are we")

        phase_map = self.get_or_init_phases()
        active = self.get_active_phase(phase_map)
        if not active:
            return "All phases complete. Project is done."

        merged_prs = self._get_merged_prs(limit=30)
        pr_context = self._format_pr_context(merged_prs)
        cache_key = f"active_phase:{active.get('id')}:{active.get('status')}:{len(merged_prs)}"
        cached = self._cached(cache_key)
        if cached:
            return cached

        prompt = f"""Give Cheuck a one-paragraph update on where Phase {active['id']} stands.

Phase: {active['name']}
Description: {active['description']}
Expected deliverables: {json.dumps(active.get('deliverables', []))}

Recent merged PRs:
{pr_context}

Based on the PRs, what has been built so far in this phase and what's still missing?
Plain English. 3-4 sentences. No jargon."""

        response = self._create_chat_completion(
            "pm_briefing",
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=250,
        )
        return self._store_cache(
            cache_key,
            f"*Phase {active['id']}: {active['name']}*\n\n" + response.choices[0].message.content.strip(),
        )

    def get_whats_left(self) -> str:
        """Gap analysis — what's specifically still missing in the active phase."""
        if not self.ai_briefings_enabled:
            return self._deterministic_checkpoint_summary("What's left")

        phase_map = self.get_or_init_phases()
        active = self.get_active_phase(phase_map)
        if not active:
            return "Nothing left. All phases are complete."

        merged_prs = self._get_merged_prs(limit=30)
        pr_context = self._format_pr_context(merged_prs)
        cache_key = f"whats_left:{active.get('id')}:{active.get('status')}:{len(merged_prs)}"
        cached = self._cached(cache_key)
        if cached:
            return cached

        prompt = f"""Based on merged PRs, identify exactly what's still missing for Phase {active['id']} to be complete.

Phase: {active['name']}
Expected deliverables: {json.dumps(active.get('deliverables', []))}

Merged PRs:
{pr_context}

List only what's genuinely missing — not done yet. Be specific.
Format as a short bullet list. Plain English. No file names."""

        response = self._create_chat_completion(
            "pm_briefing",
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        return self._store_cache(
            cache_key,
            f"*What's still needed for Phase {active['id']}:*\n\n" + response.choices[0].message.content.strip(),
        )

    def get_weekly_summary(self) -> str:
        """Monday morning summary with velocity and cost context."""
        if not self.ai_briefings_enabled:
            return self._deterministic_checkpoint_summary("Weekly summary")

        merged_prs = self._get_merged_prs(limit=30)
        phase_map = self.get_or_init_phases()
        open_issues = self._get_open_issues()
        velocity = self._calculate_velocity(merged_prs)

        pr_context = self._format_pr_context(merged_prs)
        phases_context = self._format_phases_context(phase_map)

        prompt = f"""Generate a Monday morning project summary for Cheuck.

{phases_context}

RECENT MERGED PRs:
{pr_context}

Velocity: {velocity['prs_per_day']} PRs/day over last 7 days.
Last merge: {velocity.get('last_merge_at', 'Unknown')}
Open issues in backlog: {len(open_issues)}
{"⚠️ Build stalled — no merges in " + str(velocity['days_since_last_merge']) + " days." if velocity['is_stalled'] else ""}

Tell him:
1. What got done this week (plain English)
2. Where the project stands overall
3. What's coming next
4. Any flags or risks

Brief. Conversational. Max 6 sentences."""

        response = self._create_chat_completion(
            "weekly_summary",
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PM_SYSTEM_PROMPT + "\n\n" + PROJECT_CONTEXT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=350,
        )
        return response.choices[0].message.content.strip()

    def create_next_issue_brief(self, milestone_hint: str = "") -> dict | None:
        """Generate a detailed GitHub Issue brief for the builder."""
        merged_prs = self._get_merged_prs(limit=30)
        phase_map = self.get_or_init_phases()
        open_issues = self._get_open_issues()

        pr_context = self._format_pr_context(merged_prs)
        phases_context = self._format_phases_context(phase_map)

        existing_issues = ""
        if open_issues:
            existing_issues = "ALREADY OPEN ISSUES (do not duplicate):\n" + "\n".join([
                f"- {i['title']}" for i in open_issues[:10]
            ])

        prompt = f"""Create a detailed GitHub Issue brief for the next task in this project.

{phases_context}

RECENT MERGED PRs (what's already been built):
{pr_context}

{existing_issues}

{"Cheuck specified: " + milestone_hint if milestone_hint else "Determine the most valuable next task automatically based on phase progress."}

Return valid JSON only. No markdown fences:
{{
  "title": "Technical issue title",
  "body": "Full detailed brief for the AI builder",
  "milestone": "Plain English name of this milestone"
}}

The body must include:
- Goal (what this achieves in business terms)
- Success criteria (how to know it's done)
- Scope (what to change, what NOT to touch)
- Safety classification (auto-merge safe or HOLD required)
- Cheuck action needed
- Manual setup needed
- Credential setup needed
- Cost risk
- Live-system risk
- Business-logic risk
- Do-not-do warnings
- Expected files to change
- Test requirements
- Rollback plan

Detailed enough that the AI builder can implement without asking any questions."""

        try:
            response = self._create_chat_completion(
                "pm_briefing",
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": PM_SYSTEM_PROMPT + "\n\n" + PROJECT_CONTEXT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=2500,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Issue brief generation failed: {e}")
            return None

    def parse_intent(self, text: str, last_context: str = "") -> str:
        """
        Use GPT-4o to parse unknown Telegram messages and route to the right action.
        Returns one of: whats_next, kickoff, phases, where_are_we, whats_left,
                        approve_phase, weekly_summary, status, unknown
        """
        if not self.intent_parser_enabled:
            return "unknown"

        prompt = f"""A user sent this message to a project management bot: "{text}"

Last thing the bot said: "{last_context}"

Map this message to one of these actions:
- whats_next: asking for project status or what to do next
- kickoff: agreeing to start the next task (yes, ok, go ahead, jalan, boleh, can, confirm etc)
- phases: asking to see all phases
- where_are_we: asking about current phase or overall progress
- whats_left: asking what's still missing or remaining
- approve_phase: approving a completed phase to move to the next (says "approved" after a phase completion message)
- weekly_summary: asking for weekly summary
- status: asking about tower health/stats
- unknown: none of the above

Return JSON only: {{"action": "action_name", "confidence": "high|medium|low"}}"""

        try:
            response = self._create_chat_completion(
                "intent_parser",
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=100,
            )
            result = json.loads(response.choices[0].message.content)
            if result.get("confidence") in ("high", "medium"):
                return result.get("action", "unknown")
            return "unknown"
        except Exception:
            return "unknown"
