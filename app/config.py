"""Runtime configuration helpers for Control Tower."""

from __future__ import annotations

import os


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


AI_FLAGS = {
    "ENABLE_PR_AI_REVIEW": env_bool("ENABLE_PR_AI_REVIEW", False),
    "ENABLE_PM_AI_BRIEFINGS": env_bool("ENABLE_PM_AI_BRIEFINGS", False),
    "ENABLE_BACKGROUND_AI": env_bool("ENABLE_BACKGROUND_AI", False),
    "ENABLE_AI_INTENT_PARSER": env_bool("ENABLE_AI_INTENT_PARSER", False),
    "ENABLE_WEEKLY_AI_SUMMARY": env_bool("ENABLE_WEEKLY_AI_SUMMARY", False),
}

PM_AI_CACHE_TTL_SECONDS = env_int("PM_AI_CACHE_TTL_SECONDS", 900)


def format_ai_flags_for_status() -> str:
    labels = {
        "ENABLE_PR_AI_REVIEW": "PR AI review",
        "ENABLE_PM_AI_BRIEFINGS": "PM AI briefing",
        "ENABLE_BACKGROUND_AI": "Background AI",
        "ENABLE_AI_INTENT_PARSER": "Intent parser",
        "ENABLE_WEEKLY_AI_SUMMARY": "Weekly AI summary",
    }
    lines = ["*AI cost controls:*"]
    for key, label in labels.items():
        lines.append(f"- {label}: {'enabled' if AI_FLAGS[key] else 'disabled'}")
    if AI_FLAGS["ENABLE_BACKGROUND_AI"] or AI_FLAGS["ENABLE_WEEKLY_AI_SUMMARY"]:
        lines.append("- Background AI may consume OpenAI credits.")
    return "\n".join(lines)
