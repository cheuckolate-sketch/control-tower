"""
main.py
Control Tower — the orchestrator.
Polls GitHub, reviews PRs, pings Cheuck on Telegram, handles approvals.
"""

import asyncio
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

from app.github_client import GitHubClient
from app.reviewer import AIReviewer
from app.telegram_bot import TelegramNotifier, TelegramCommandHandler
from app.state import StateTracker

load_dotenv()

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


class ControlTower:
    def __init__(self):
        self.github = GitHubClient(GITHUB_TOKEN, GITHUB_REPO)
        self.reviewer = AIReviewer(OPENAI_API_KEY, OPENAI_MODEL)
        self.reviewer.daily_limit = DAILY_LIMIT
        self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.state = StateTracker()
        self.cmd_handler = TelegramCommandHandler(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            self.handle_telegram_action
        )
        self.running = False

    async def handle_telegram_action(self, action: str, pr_number: int, update):
        """Handle approve/reject/details/skip commands from Cheuck."""
        from telegram.constants import ParseMode

        if action == "status":
            stats = self.state.get_status()
            msg = f"""
⬡ *Control Tower Status*

PRs reviewed today: {stats['daily_stats']['prs_reviewed']}
PRs merged today: {stats['daily_stats']['prs_merged']}
PRs skipped: {stats['skipped_count']}
OpenAI calls today: {stats['daily_stats']['openai_calls']}
Poll interval: every {POLL_INTERVAL}s
Repo: `{GITHUB_REPO}`
"""
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return

        pr = self.github.get_pr_by_number(pr_number)
        if not pr:
            await update.message.reply_text(f"PR #{pr_number} not found.")
            return

        if action == "approve":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                await self.notifier.send_merge_success(pr_number, pr.title)
                await update.message.reply_text(f"✅ PR #{pr_number} merged. Railway deploying.")
            else:
                await update.message.reply_text(f"❌ Merge failed for PR #{pr_number}. Check GitHub directly.")

        elif action == "reject":
            pr.edit(state="closed")
            await update.message.reply_text(f"🚫 PR #{pr_number} closed without merge.")

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

            msg = f"*PR #{pr_number} Details*\n\n*Files:*\n{files_text or 'None'}\n\n*Checks:*\n{checks_text or 'None'}\n\n[View on GitHub]({pr.html_url})"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

        elif action == "skip":
            self.state.mark_skipped(pr_number)
            await update.message.reply_text(f"⏭ PR #{pr_number} skipped. Won't alert you again unless you unskip.")

    async def process_pr(self, pr):
        """Full review pipeline for a single PR."""
        pr_number = pr.number

        # Skip drafts
        if pr.draft:
            logger.info(f"PR #{pr_number} is a draft. Skipping.")
            return

        # Skip if already reviewed or skipped
        if self.state.has_been_reviewed(pr_number):
            logger.debug(f"PR #{pr_number} already reviewed. Skipping.")
            return

        if self.state.is_skipped(pr_number):
            logger.debug(f"PR #{pr_number} is skipped by user.")
            return

        logger.info(f"Processing PR #{pr_number}: {pr.title}")

        # Get full PR details
        details = self.github.get_pr_details(pr)
        if not details:
            logger.error(f"Failed to get details for PR #{pr_number}")
            return

        # Check CI status first
        failed_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("conclusion") in ["failure", "cancelled", "timed_out"]
        ]

        pending_checks = [
            c["name"] for c in details.get("check_runs", [])
            if c.get("status") in ["in_progress", "queued"]
        ]

        # If checks still running, wait for them
        if pending_checks and not failed_checks:
            logger.info(f"PR #{pr_number} has pending checks: {pending_checks}. Will review next poll.")
            return

        # If checks failed, alert Cheuck
        if failed_checks:
            await self.notifier.send_ci_failure_alert(
                pr_number, pr.title, pr.html_url, failed_checks
            )
            self.state.mark_reviewed(pr_number, "CI_FAILED")
            return

        # Run AI review
        review = self.reviewer.review_pr(details)
        decision = review.get("decision", "HOLD")

        # Build GitHub comment
        decision_emoji = {"MERGE": "✅", "FIX": "🔧", "HOLD": "⛔"}.get(decision, "❓")
        risks_md = "\n".join([f"- {r}" for r in review.get("risks", [])]) or "None"

        comment_body = f"""{BOT_MARKER}
## {decision_emoji} Control Tower Review — {decision}

**Summary:** {review.get('summary', '')}

**Reasoning:** {review.get('reasoning', '')}

**Risks:**
{risks_md}

**Confidence:** {review.get('confidence', 'unknown')}

---
*Reviewed at {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} · Waiting for Cheuck approval*
"""

        if review.get("fix_instructions"):
            comment_body += f"\n**Fix Instructions for Codex:**\n{review['fix_instructions']}"

        # Post to GitHub
        existing = self.github.get_existing_bot_comment(pr, BOT_MARKER)
        if existing:
            self.github.update_pr_comment(existing, comment_body)
        else:
            self.github.post_pr_comment(pr, comment_body)

        # Mark as reviewed
        self.state.mark_reviewed(pr_number, decision)

        # Send Telegram alert
        await self.notifier.send_pr_alert(
            pr_number=pr_number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            decision=decision,
            summary=review.get("summary", ""),
            reasoning=review.get("reasoning", ""),
            risks=review.get("risks", []),
            human_reason=review.get("human_approval_reason", "")
        )

        logger.info(f"PR #{pr_number} processed. Decision: {decision}")

    async def poll_loop(self):
        """Main polling loop — checks GitHub every N seconds."""
        logger.info(f"Starting poll loop. Interval: {POLL_INTERVAL}s")

        while self.running:
            try:
                prs = self.github.get_open_prs()
                logger.info(f"Found {len(prs)} open PR(s)")

                for pr in prs:
                    await self.process_pr(pr)

            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await self.notifier.send_message(f"⚠️ Control Tower error: {str(e)[:200]}")

            await asyncio.sleep(POLL_INTERVAL)

    async def run(self):
        """Start everything."""
        logger.info("Control Tower starting up...")

        # First run: get chat ID if not set
        if not TELEGRAM_CHAT_ID:
            logger.info("No TELEGRAM_CHAT_ID set. Attempting to detect...")
            chat_id = await self.notifier.get_chat_id()
            if not chat_id:
                logger.error("Could not detect chat ID. Send a message to your bot and restart.")
                return
            self.notifier.chat_id = chat_id
            self.cmd_handler.chat_id = chat_id

        self.running = True

        # Start Telegram command listener
        await self.cmd_handler.start()

        # Send startup ping
        await self.notifier.send_startup_message()

        # Start poll loop
        await self.poll_loop()

    async def shutdown(self):
        self.running = False
        await self.cmd_handler.stop()
        logger.info("Control Tower shut down.")


async def main():
    tower = ControlTower()
    try:
        await tower.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await tower.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
