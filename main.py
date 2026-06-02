"""
main.py
Control Tower — orchestrator.
Polls GitHub, reviews PRs, pings Cheuck on Telegram, handles approvals.
Uses sync Telegram (v13.x) + threaded polling.
"""

import logging
import os
import time
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

    def handle_telegram_action(self, action: str, pr_number, update):
        if action == "status":
            stats = self.state.get_status()
            msg = (
                f"⬡ *Control Tower Status*\n\n"
                f"PRs reviewed today: {stats['daily_stats']['prs_reviewed']}\n"
                f"PRs merged today: {stats['daily_stats']['prs_merged']}\n"
                f"PRs skipped: {stats['skipped_count']}\n"
                f"Repo: `{GITHUB_REPO}`"
            )
            update.message.reply_text(msg, parse_mode="Markdown")
            return

        pr = self.github.get_pr_by_number(pr_number)
        if not pr:
            update.message.reply_text(f"PR #{pr_number} not found.")
            return

        if action == "approve":
            success = self.github.merge_pr(pr)
            if success:
                self.state.mark_merged(pr_number)
                self.notifier.send_merge_success(pr_number, pr.title)
                update.message.reply_text(f"✅ PR #{pr_number} merged. Railway deploying.")
            else:
                update.message.reply_text(f"❌ Merge failed for PR #{pr_number}. Check GitHub.")

        elif action == "reject":
            pr.edit(state="closed")
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
            msg = f"*PR #{pr_number} Details*\n\n*Files:*\n{files_text or 'None'}\n\n*Checks:*\n{checks_text or 'None'}\n\n[View on GitHub]({pr.html_url})"
            update.message.reply_text(msg, parse_mode="Markdown")

        elif action == "skip":
            self.state.mark_skipped(pr_number)
            update.message.reply_text(f"⏭ PR #{pr_number} skipped.")

    def process_pr(self, pr):
        pr_number = pr.number

        if pr.draft:
            logger.info(f"PR #{pr_number} is draft. Skipping.")
            return

        if self.state.has_been_reviewed(pr_number):
            logger.debug(f"PR #{pr_number} already reviewed.")
            return

        if self.state.is_skipped(pr_number):
            logger.debug(f"PR #{pr_number} skipped by user.")
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
            logger.info(f"PR #{pr_number} has pending checks. Waiting.")
            return

        if failed_checks:
            self.notifier.send_ci_failure_alert(pr_number, pr.title, pr.html_url, failed_checks)
            self.state.mark_reviewed(pr_number, "CI_FAILED")
            return

        review = self.reviewer.review_pr(details)
        decision = review.get("decision", "HOLD")

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
*Reviewed at {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}*
"""
        if review.get("fix_instructions"):
            comment_body += f"\n**Fix Instructions for Codex:**\n{review['fix_instructions']}"

        existing = self.github.get_existing_bot_comment(pr, BOT_MARKER)
        if existing:
            self.github.update_pr_comment(existing, comment_body)
        else:
            self.github.post_pr_comment(pr, comment_body)

        self.state.mark_reviewed(pr_number, decision)

        self.notifier.send_pr_alert(
            pr_number=pr_number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            decision=decision,
            summary=review.get("summary", ""),
            reasoning=review.get("reasoning", ""),
            risks=review.get("risks", []),
            human_reason=review.get("human_approval_reason", "")
        )

        logger.info(f"PR #{pr_number} done. Decision: {decision}")

    def run(self):
        logger.info("Control Tower starting...")

        if not TELEGRAM_CHAT_ID:
            chat_id = self.notifier.get_chat_id()
            if not chat_id:
                logger.error("No chat ID. Send a message to your bot and restart.")
                return
            self.notifier.chat_id = chat_id
            self.cmd_handler.chat_id = chat_id

        # Start Telegram polling in background thread
        self.cmd_handler.start()

        # Send startup ping
        self.notifier.send_startup_message()

        # Main poll loop
        while True:
            try:
                prs = self.github.get_open_prs()
                logger.info(f"Found {len(prs)} open PR(s)")
                for pr in prs:
                    self.process_pr(pr)
            except Exception as e:
                logger.error(f"Poll error: {e}")
                self.notifier.send_message(f"⚠️ Control Tower error: {str(e)[:200]}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    tower = ControlTower()
    tower.run()
