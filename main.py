"""
main.py
Control Tower V3 — orchestrator.

V3 additions:
- ProjectManager replaces ProjectPlanner (unified PM intelligence)
- Phase commands: phases, where_are_we, whats_left, approve_phase
- Conversation state (yes/approved know what they're responding to)
- Stall detection in poll loop (pings Cheuck if no PRs in 48hrs)
- HOLD escalation follow-up (once, after 4 hours)
- Builder PR notifications (pings when ai-built PR opens)
- Weekly summary now fetches real merged PRs
V3.1 fix:
- phase_completion_pinged flag prevents double phase complete alerts
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from app.github_client import GitHubClient
from app.reviewer import AIReviewer
from app.telegram_bot import TelegramNotifier, TelegramCommandHandler
from app.state import StateTracker
from app.project_manager import ProjectManager
from app.config import AI_FLAGS, PM_AI_CACHE_TTL_SECONDS, format_ai_flags_for_status
from app.operator import build_operator_snapshot, checkpoint_is_stale

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("control_tower.log")
    ]
)
logger = logging.getLogger("ControlTower")

# ── CONFIG ──
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "cheuckolate-sketch/creator-campaign-os-backend")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DAILY_LIMIT = int(os.getenv("DAILY_OPENAI_CALL_LIMIT", "50"))

BOT_MARKER = "<!-- control-tower-review -->"

# How long to wait before following up on an unanswered HOLD (seconds)
HOLD_FOLLOWUP_DELAY = 4 * 60 * 60  # 4 hours

# How long between stall checks (seconds) — only ping once per stall period
STALL_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours


class ControlTower:
    def __init__(self):
        self.github = GitHubClient(GITHUB_TOKEN, GITHUB_REPO)
        self.state = StateTracker()
        self.reviewer = AIReviewer(
            OPENAI_API_KEY,
            OPENAI_MODEL,
            enabled=AI_FLAGS["ENABLE_PR_AI_REVIEW"],
        )
        self.reviewer.daily_limit = DAILY_LIMIT
        self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.pm = ProjectManager(
            github_client=self.github,
            ai_briefings_enabled=AI_FLAGS["ENABLE_PM_AI_BRIEFINGS"],
            intent_parser_enabled=AI_FLAGS["ENABLE_AI_INTENT_PARSER"],
            cache_ttl_seconds=PM_AI_CACHE_TTL_SECONDS,
            call_recorder=self.state.record_openai_call,
        )

        # Conversation state
        self.pending_kickoff = False        # waiting for Cheuck to say "yes" after whats_next
        self.pending_phase_id = None        # waiting for "approved" after phase complete alert
        self.phase_completion_pinged = False # prevents double-pinging on same phase completion

        # HOLD escalation tracking: {pr_number: datetime when HOLD was sent}
        self.hold_sent_at: dict[int, datetime] = {}
        self.hold_followed_up: set[int] = set()

        # Stall tracking
        self.last_stall_ping: datetime | None = None
        self.stall_detected: bool = False

        # Notified builder PRs (avoid double-pinging)
        self.notified_builder_prs: set[int] = set()

        self.cmd_handler = TelegramCommandHandler(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            self.handle_telegram_action,
            project_manager=self.pm,
            intent_parser_enabled=AI_FLAGS["ENABLE_AI_INTENT_PARSER"],
        )

    def build_operator_snapshot_message(self, known_blocker: str = "Not verified") -> str:
        latest_merged = self.github.get_merged_prs_with_diffs(limit=3)
        open_prs = self.github.get_open_pr_summaries(limit=5)
        open_issues = self.github.get_open_issue_summaries(limit=5)
        closed_unmerged = self.github.get_latest_closed_unmerged_pr()
        checkpoint = self.state.get_latest_runtime_checkpoint()
        active_phase = self.pm.get_active_phase_snapshot()
        msg = build_operator_snapshot(
            repo_name=GITHUB_REPO,
            phase=active_phase,
            latest_merged_prs=latest_merged,
            open_prs=open_prs,
            open_issues=open_issues,
            latest_closed_unmerged_pr=closed_unmerged,
            runtime_checkpoint=checkpoint,
            known_blocker=known_blocker,
        )
        if checkpoint_is_stale(checkpoint, latest_merged):
            msg += "\n\nRuntime checkpoint may be stale. Rerun the relevant endpoint or add a checkpoint before making the next decision."
        return msg

    # ─────────────────────────────────────────────
    # TELEGRAM ACTION HANDLER
    # ─────────────────────────────────────────────

    def handle_telegram_action(self, action: str, payload, update):

        # ── STATUS ──
        if action == "status":
            activity = self.github.get_recent_activity_summary()
            stats = self.state.get_status()
            counts = stats["daily_stats"].get("openai_calls_by_category", {})
            counts_text = "\n".join([
                f"- {category}: {counts.get(category, 0)}"
                for category in ["pr_review", "pm_briefing", "intent_parser", "background_ai", "weekly_summary"]
            ])
            msg = (
                f"⬡ *Control Tower V3 Status*\n\n"
                f"PRs reviewed today: {stats['daily_stats']['prs_reviewed']}\n"
                f"PRs merged today: {stats['daily_stats']['prs_merged']}\n"
                f"PRs skipped: {stats['skipped_count']}\n"
                f"Open PRs: {activity.get('open_prs', '?')}\n"
                f"Open issues: {activity.get('open_issues', '?')}\n"
                f"Last merge: {activity.get('last_merge_at', 'Unknown')}\n"
                f"Repo: `{GITHUB_REPO}`\n\n"
                f"*OpenAI calls today:*\n{counts_text}\n\n"
                f"{format_ai_flags_for_status()}\n\n"
                f"{self.build_operator_snapshot_message()}"
            )
            update.message.reply_text(msg, parse_mode="Markdown")
            return

        if action == "checkpoint":
            checkpoint = self.state.add_runtime_checkpoint(str(payload))
            update.message.reply_text(
                "Checkpoint recorded. I will include it in `status`, `what's next`, and `where are we`.\n\n"
                "Reminder: do not paste secrets, API keys, or tokens into checkpoints."
            )
            self.cmd_handler.set_last_context("checkpoint", checkpoint["text"])
            return

        # ── WHAT'S NEXT ──
        if action == "whats_next":
            try:
                snapshot = self.build_operator_snapshot_message()
                response = self.pm.get_full_briefing()
                self.pending_kickoff = True
                full_response = f"{snapshot}\n\n{response}"
                self.cmd_handler.set_last_context("whats_next", full_response)
                update.message.reply_text(full_response, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"whats_next error: {e}")
                update.message.reply_text("Having trouble checking status right now. Try again in a moment.")
            return

        # ── KICKOFF ──
        if action == "kickoff":
            if not self.pending_kickoff:
                update.message.reply_text("Send `what's next` first so I know what to kick off.")
                return
            try:
                update.message.reply_text("Creating the GitHub Issue and briefing the builder...")
                brief = self.pm.create_next_issue_brief()
                if not brief:
                    update.message.reply_text("Couldn't generate the issue brief. Try again.")
                    return

                open_issues = self.github.get_open_issue_summaries(limit=20)
                brief_title = brief["title"].strip().lower()
                duplicate_issue = next(
                    (
                        issue for issue in open_issues
                        if brief_title and (
                            brief_title == issue["title"].strip().lower()
                            or brief_title in issue["title"].strip().lower()
                            or issue["title"].strip().lower() in brief_title
                        )
                    ),
                    None,
                )
                if duplicate_issue:
                    self.pending_kickoff = False
                    update.message.reply_text(
                        f"Similar open issue found: #{duplicate_issue['number']} {duplicate_issue['title']}\n\n"
                        "Next safe action: continue the existing issue, close/rewrite it, or create a narrower follow-up only if genuinely different."
                    )
                    return

                issue = self.github.create_issue(
                    title=brief["title"],
                    body=brief["body"]
                )
                if issue:
                    self.pending_kickoff = False
                    msg = (
                        f"✅ *Issue #{issue.number} created*\n\n"
                        f"_{brief['milestone']}_\n\n"
                        f"Builder will pick this up automatically via GitHub Actions. "
                        f"I'll ping you when the PR is ready."
                    )
                    self.cmd_handler.set_last_context("kickoff", msg)
                    update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    update.message.reply_text("Failed to create GitHub Issue. Check GitHub directly.")
            except Exception as e:
                logger.error(f"Kickoff error: {e}")
                update.message.reply_text(f"Something went wrong: {str(e)[:200]}")
            return

        # ── PHASES ──
        if action == "phases":
            try:
                summary = self.pm.get_phase_summary()
                self.cmd_handler.set_last_context("phases", summary)
                update.message.reply_text(summary, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Phases error: {e}")
                update.message.reply_text("Couldn't load phase map right now.")
            return

        # ── WHERE ARE WE ──
        if action == "where_are_we":
            try:
                snapshot = self.build_operator_snapshot_message()
                detail = self.pm.get_active_phase_detail()
                full_detail = f"{snapshot}\n\n{detail}"
                self.cmd_handler.set_last_context("where_are_we", full_detail)
                update.message.reply_text(full_detail, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Where are we error: {e}")
                update.message.reply_text("Couldn't check current phase right now.")
            return

        # ── WHAT'S LEFT ──
        if action == "whats_left":
            try:
                snapshot = self.build_operator_snapshot_message()
                gap = self.pm.get_whats_left()
                full_gap = f"{snapshot}\n\n{gap}"
                self.cmd_handler.set_last_context("whats_left", full_gap)
                update.message.reply_text(full_gap, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Whats left error: {e}")
                update.message.reply_text("Couldn't run gap analysis right now.")
            return

        # ── APPROVE PHASE ──
        if action == "approve_phase":
            phase_id = payload
            if not phase_id:
                update.message.reply_text("Not sure which phase to approve. Send `phases` to check.")
                return
            try:
                msg = self.pm.approve_phase(phase_id)
                self.pending_phase_id = None
                self.phase_completion_pinged = False  # reset so next phase can be detected
                self.cmd_handler.set_pending_phase(None)
                self.cmd_handler.set_last_context("approve_phase", msg)
                update.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Approve phase error: {e}")
                update.message.reply_text("Couldn't approve phase right now. Check GitHub directly.")
            return

        # ── WEEKLY SUMMARY ──
        if action == "weekly_summary":
            if not AI_FLAGS["ENABLE_WEEKLY_AI_SUMMARY"]:
                update.message.reply_text("Weekly AI summary is disabled. Use `status` or `what's next` for deterministic status.")
                return
            try:
                summary = self.pm.get_weekly_summary()
                self.notifier.send_weekly_summary(summary)
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")
                update.message.reply_text("Couldn't generate weekly summary right now.")
            return

        # ── PR ACTIONS ──
        pr_number = payload
        pr = self.github.get_pr_by_number(pr_number)
        if not pr:
            update.message.reply_text(f"PR #{pr_number} not found.")
            return

        if action == "approve":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.hold_sent_at.pop(pr_number, None)
                self.hold_followed_up.discard(pr_number)
                self.notifier.send_merge_success(pr_number, pr.title)
                update.message.reply_text(f"✅ PR #{pr_number} merged. Railway deploying.")
            else:
                update.message.reply_text(f"❌ Merge failed for PR #{pr_number}. Check GitHub.")

        elif action == "reject":
            pr.edit(state="closed")
            self.hold_sent_at.pop(pr_number, None)
            self.hold_followed_up.discard(pr_number)
            update.message.reply_text(f"🚫 PR #{pr_number} closed without merge.")

        elif action == "details":
            details = self.github.get_pr_details(pr)
            files = details.get("files_changed", [])
            checks = details.get("check_runs", [])
            files_text = "\n".join([
                f"• `{f['filename']}` ({f['status']}, +{f['additions']} -{f['deletions']})"
                for f in files[:15]
            ])
            checks_text = "\n".join([
                f"• {c['name']}: {c.get('conclusion') or c.get('status')}"
                for c in checks
            ])
            msg = (
                f"*PR #{pr_number} Details*\n\n"
                f"*Files:*\n{files_text or 'None'}\n\n"
                f"*Checks:*\n{checks_text or 'None'}\n\n"
                f"[View on GitHub]({pr.html_url})"
            )
            update.message.reply_text(msg, parse_mode="Markdown")

        elif action == "skip":
            self.state.mark_skipped(pr_number)
            self.hold_sent_at.pop(pr_number, None)
            update.message.reply_text(f"⏭ PR #{pr_number} skipped.")

    # ─────────────────────────────────────────────
    # PR PROCESSING
    # ─────────────────────────────────────────────

    def process_pr(self, pr):
        pr_number = pr.number

        if pr.draft:
            return

        if self.state.is_skipped(pr_number):
            return

        # Notify when builder PRs open (ai-built label)
        labels = [l.name for l in pr.labels]
        if "ai-built" in labels and pr_number not in self.notified_builder_prs:
            self.notifier.send_builder_pr_opened(pr_number, pr.title, pr.html_url, pr_number)
            self.notified_builder_prs.add(pr_number)

        if self.state.has_been_reviewed(pr_number):
            return

        logger.info(f"Processing PR #{pr_number}: {pr.title}")

        details = self.github.get_pr_details(pr)
        if not details:
            return

        failed_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("conclusion") in ["failure", "cancelled", "timed_out"]
        ]
        pending_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("status") in ["in_progress", "queued"]
        ]

        if pending_checks and not failed_checks:
            return

        if failed_checks:
            self.notifier.send_ci_failure_alert(pr_number, pr.title, pr.html_url, failed_checks)
            self.state.mark_reviewed(pr_number, "CI_FAILED")
            return

        review = self.reviewer.review_pr(details)
        if self.reviewer.last_openai_call_made:
            self.state.record_openai_call("pr_review")
        decision = review.get("decision", "HOLD")
        hold_trigger = review.get("hold_trigger", "none")

        # Post GitHub comment
        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔", "AUTO_MERGE": "✅"}.get(decision, "❓")
        risks_md = "\n".join([f"- {r}" for r in review.get("risks", [])]) or "None"

        comment_body = f"""{BOT_MARKER}
## {decision_emoji} Control Tower Review — {decision}

**Summary:** {review.get('summary', '')}

**Reasoning:** {review.get('reasoning', '')}

**Risks:**
{risks_md}

**Confidence:** {review.get('confidence', 'unknown')}
**Hold Trigger:** {hold_trigger}

---
*Reviewed at {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} · Control Tower V3*
"""
        if review.get("fix_instructions"):
            comment_body += f"\n**Fix Instructions:**\n{review['fix_instructions']}"

        existing = self.github.get_existing_bot_comment(pr, BOT_MARKER)
        if existing:
            self.github.update_pr_comment(existing, comment_body)
        else:
            self.github.post_pr_comment(pr, comment_body)

        self.state.mark_reviewed(pr_number, decision)

        # ── AUTO-MERGE ──
        if decision == "AUTO_MERGE" and review.get("confidence") == "high":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.notifier.send_auto_merge_notification(pr_number, pr.title)
            else:
                self.notifier.send_message(
                    f"⚠️ Auto-merge failed for PR #{pr_number}: {pr.title}\n\n"
                    f"`approve {pr_number}` to merge manually."
                )
            return

        # ── HOLD — track for escalation follow-up ──
        if decision == "HOLD" and review.get("human_approval_required"):
            self.hold_sent_at[pr_number] = datetime.now(timezone.utc)

        self.notifier.send_pr_alert(
            pr_number=pr_number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            decision=decision,
            summary=review.get("summary", ""),
            reasoning=review.get("reasoning", ""),
            risks=review.get("risks", []),
            human_reason=review.get("human_approval_reason", ""),
            hold_trigger=hold_trigger,
            files_changed=details.get("files_changed", []),
            check_runs=details.get("check_runs", []),
        )

    # ─────────────────────────────────────────────
    # PROACTIVE CHECKS (run every poll cycle)
    # ─────────────────────────────────────────────

    def check_hold_escalations(self):
        """Follow up once on HOLD alerts that haven't been actioned in 4 hours."""
        now = datetime.now(timezone.utc)
        for pr_number, sent_at in list(self.hold_sent_at.items()):
            if pr_number in self.hold_followed_up:
                continue
            if (now - sent_at).total_seconds() >= HOLD_FOLLOWUP_DELAY:
                pr = self.github.get_pr_by_number(pr_number)
                if pr and pr.state == "open":
                    self.notifier.send_hold_followup(pr_number, pr.title)
                    self.hold_followed_up.add(pr_number)
                else:
                    self.hold_sent_at.pop(pr_number, None)

    def check_stall(self):
        """Ping Cheuck if no PRs merged in 48 hours. Only pings once per stall period."""
        now = datetime.now(timezone.utc)

        if self.last_stall_ping and (now - self.last_stall_ping).total_seconds() < STALL_CHECK_INTERVAL:
            return

        stall_msg = self.pm.check_for_stall()
        if stall_msg:
            if not self.stall_detected:
                self.notifier.send_message(stall_msg)
                self.last_stall_ping = now
                self.stall_detected = True
        else:
            self.stall_detected = False

    def check_phase_completion(self):
        """Check if active phase looks done. Alert Cheuck and wait for 'approved'."""
        if not AI_FLAGS["ENABLE_BACKGROUND_AI"]:
            logger.info("Skipping phase completion check because ENABLE_BACKGROUND_AI=false.")
            return
        if self.pending_phase_id or self.phase_completion_pinged:
            return  # already waiting for approval or already pinged

        try:
            is_complete, active_phase, msg = self.pm.check_phase_completion()
            if is_complete and active_phase:
                self.pending_phase_id = active_phase["id"]
                self.phase_completion_pinged = True
                self.cmd_handler.set_pending_phase(active_phase["id"])
                self.notifier.send_phase_complete_alert(
                    active_phase["id"],
                    active_phase["name"],
                    msg
                )
        except Exception as e:
            logger.error(f"Phase completion check error: {e}")

    def send_weekly_summary_if_monday(self):
        """Send weekly summary automatically on Monday at 9am."""
        if not AI_FLAGS["ENABLE_WEEKLY_AI_SUMMARY"]:
            return
        now = datetime.now()
        if now.weekday() == 0 and now.hour == 9 and now.minute < 1:
            try:
                summary = self.pm.get_weekly_summary()
                self.notifier.send_weekly_summary(summary)
                logger.info("Weekly summary sent.")
            except Exception as e:
                logger.error(f"Weekly summary error: {e}")

    # ─────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────

    def run(self):
        logger.info("Control Tower V3 starting...")

        if not TELEGRAM_CHAT_ID:
            chat_id = self.notifier.get_chat_id()
            if not chat_id:
                logger.error("No chat ID. Send a message to your bot and restart.")
                return
            self.notifier.chat_id = chat_id
            self.cmd_handler.chat_id = chat_id

        if AI_FLAGS["ENABLE_BACKGROUND_AI"]:
            try:
                self.pm.get_or_init_phases()
                logger.info("Phase map ready.")
            except Exception as e:
                logger.error(f"Phase map init failed: {e}")
        else:
            logger.info("Skipping phase map init because ENABLE_BACKGROUND_AI=false.")

        self.cmd_handler.start()
        self.notifier.send_startup_message()

        poll_count = 0

        while True:
            try:
                # ── PR REVIEW LOOP ──
                prs = self.github.get_open_prs()
                logger.info(f"Found {len(prs)} open PR(s)")
                for pr in prs:
                    self.process_pr(pr)

                # ── PROACTIVE CHECKS (every 5 poll cycles to reduce API calls) ──
                poll_count += 1
                if poll_count % 5 == 0:
                    self.check_hold_escalations()
                    self.check_stall()

                # ── PHASE COMPLETION CHECK (every 10 poll cycles) ──
                if poll_count % 10 == 0:
                    self.check_phase_completion()

                # ── WEEKLY SUMMARY ──
                self.send_weekly_summary_if_monday()

            except Exception as e:
                logger.error(f"Poll error: {e}")
                self.notifier.send_message(f"⚠️ Control Tower error: {str(e)[:200]}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    tower = ControlTower()
    tower.run()
