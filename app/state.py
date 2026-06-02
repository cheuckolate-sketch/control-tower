"""
state.py
Tracks which PRs have been reviewed so we don't spam reviews on every poll.
Persists to a local JSON file so state survives restarts.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

STATE_FILE = "control_tower_state.json"


class StateTracker:
    def __init__(self):
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "reviewed_prs": {},    # pr_number -> {decision, timestamp, comment_id}
            "skipped_prs": [],     # pr numbers to ignore
            "daily_stats": {
                "date": str(datetime.now().date()),
                "openai_calls": 0,
                "prs_reviewed": 0,
                "prs_merged": 0
            }
        }

    def _save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def has_been_reviewed(self, pr_number: int) -> bool:
        return str(pr_number) in self.state["reviewed_prs"]

    def is_skipped(self, pr_number: int) -> bool:
        return pr_number in self.state["skipped_prs"]

    def mark_reviewed(self, pr_number: int, decision: str, comment_id: int = None):
        self.state["reviewed_prs"][str(pr_number)] = {
            "decision": decision,
            "timestamp": str(datetime.now()),
            "comment_id": comment_id
        }
        self._check_daily_reset()
        self.state["daily_stats"]["prs_reviewed"] += 1
        self._save()

    def mark_skipped(self, pr_number: int):
        if pr_number not in self.state["skipped_prs"]:
            self.state["skipped_prs"].append(pr_number)
        self._save()

    def unskip(self, pr_number: int):
        if pr_number in self.state["skipped_prs"]:
            self.state["skipped_prs"].remove(pr_number)
        self._save()

    def mark_merged(self, pr_number: int):
        self.state["daily_stats"]["prs_merged"] += 1
        self._save()

    def remove_reviewed(self, pr_number: int):
        """Remove from reviewed so it can be re-reviewed (e.g. after FIX)."""
        self.state["reviewed_prs"].pop(str(pr_number), None)
        self._save()

    def get_status(self) -> dict:
        self._check_daily_reset()
        return {
            "reviewed_count": len(self.state["reviewed_prs"]),
            "skipped_count": len(self.state["skipped_prs"]),
            "daily_stats": self.state["daily_stats"]
        }

    def _check_daily_reset(self):
        today = str(datetime.now().date())
        if self.state["daily_stats"]["date"] != today:
            self.state["daily_stats"] = {
                "date": today,
                "openai_calls": 0,
                "prs_reviewed": 0,
                "prs_merged": 0
            }
