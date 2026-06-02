"""
state.py
Tracks which PRs have been reviewed so we don't spam reviews on every poll.
Persists to a local JSON file so state survives restarts.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_FILE = "control_tower_state.json"


SECRET_VALUE_PATTERNS = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|gho|github_pat|xox[baprs])-?[A-Za-z0-9_=-]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
]

SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_]*\s*=\s*\S+",
    re.IGNORECASE,
)


def contains_checkpoint_secret(text: str) -> bool:
    """Return True when checkpoint text appears to contain a credential value."""
    if SECRET_ASSIGNMENT_PATTERN.search(text):
        return True
    return any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS)


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
            "runtime_checkpoints": [],
            "daily_stats": {
                "date": str(datetime.now().date()),
                "openai_calls": 0,
                "openai_calls_by_category": {
                    "pr_review": 0,
                    "pm_briefing": 0,
                    "intent_parser": 0,
                    "background_ai": 0,
                    "weekly_summary": 0,
                },
                "prs_reviewed": 0,
                "prs_merged": 0
            }
        }

    def _ensure_shape(self):
        self.state.setdefault("reviewed_prs", {})
        self.state.setdefault("skipped_prs", [])
        self.state.setdefault("runtime_checkpoints", [])
        self.state.setdefault("daily_stats", {})
        self.state["daily_stats"].setdefault("date", str(datetime.now().date()))
        self.state["daily_stats"].setdefault("openai_calls", 0)
        self.state["daily_stats"].setdefault("prs_reviewed", 0)
        self.state["daily_stats"].setdefault("prs_merged", 0)
        self.state["daily_stats"].setdefault("openai_calls_by_category", {})
        for category in [
            "pr_review",
            "pm_briefing",
            "intent_parser",
            "background_ai",
            "weekly_summary",
        ]:
            self.state["daily_stats"]["openai_calls_by_category"].setdefault(category, 0)

    def _save(self):
        try:
            self._ensure_shape()
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def has_been_reviewed(self, pr_number: int) -> bool:
        return str(pr_number) in self.state["reviewed_prs"]

    def is_skipped(self, pr_number: int) -> bool:
        return pr_number in self.state["skipped_prs"]

    def mark_reviewed(self, pr_number: int, decision: str, comment_id: int = None):
        self._ensure_shape()
        self.state["reviewed_prs"][str(pr_number)] = {
            "decision": decision,
            "timestamp": str(datetime.now()),
            "comment_id": comment_id
        }
        self._check_daily_reset()
        self.state["daily_stats"]["prs_reviewed"] += 1
        self._save()

    def mark_skipped(self, pr_number: int):
        self._ensure_shape()
        if pr_number not in self.state["skipped_prs"]:
            self.state["skipped_prs"].append(pr_number)
        self._save()

    def unskip(self, pr_number: int):
        if pr_number in self.state["skipped_prs"]:
            self.state["skipped_prs"].remove(pr_number)
        self._save()

    def mark_merged(self, pr_number: int):
        self._ensure_shape()
        self.state["daily_stats"]["prs_merged"] += 1
        self._save()

    def remove_reviewed(self, pr_number: int):
        """Remove from reviewed so it can be re-reviewed (e.g. after FIX)."""
        self.state["reviewed_prs"].pop(str(pr_number), None)
        self._save()

    def get_status(self) -> dict:
        self._check_daily_reset()
        self._ensure_shape()
        return {
            "reviewed_count": len(self.state["reviewed_prs"]),
            "skipped_count": len(self.state["skipped_prs"]),
            "daily_stats": self.state["daily_stats"],
            "latest_runtime_checkpoint": self.get_latest_runtime_checkpoint(),
        }

    def record_openai_call(self, category: str):
        self._check_daily_reset()
        self._ensure_shape()
        if category not in self.state["daily_stats"]["openai_calls_by_category"]:
            self.state["daily_stats"]["openai_calls_by_category"][category] = 0
        self.state["daily_stats"]["openai_calls"] += 1
        self.state["daily_stats"]["openai_calls_by_category"][category] += 1
        self._save()

    def add_runtime_checkpoint(self, text: str) -> dict:
        self._ensure_shape()
        checkpoint = {
            "text": text.strip()[:1000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        checkpoints = [checkpoint] + self.state.get("runtime_checkpoints", [])[:4]
        self.state["runtime_checkpoints"] = checkpoints
        self._save()
        return checkpoint

    def get_latest_runtime_checkpoint(self) -> dict | None:
        self._ensure_shape()
        checkpoints = self.state.get("runtime_checkpoints", [])
        if not checkpoints:
            return None
        return checkpoints[0]

    def _check_daily_reset(self):
        today = str(datetime.now().date())
        self._ensure_shape()
        if self.state["daily_stats"]["date"] != today:
            self.state["daily_stats"] = {
                "date": today,
                "openai_calls": 0,
                "openai_calls_by_category": {
                    "pr_review": 0,
                    "pm_briefing": 0,
                    "intent_parser": 0,
                    "background_ai": 0,
                    "weekly_summary": 0,
                },
                "prs_reviewed": 0,
                "prs_merged": 0
            }
