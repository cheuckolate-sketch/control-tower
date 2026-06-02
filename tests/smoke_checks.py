import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.operator import build_next_best_action
from app.state import contains_checkpoint_secret, sanitize_checkpoint_text


def _action_for_checkpoint(text: str) -> str:
    return build_next_best_action({
        "runtime_checkpoint": {"text": text},
        "latest_merged_prs": [],
        "open_prs": [],
    })


def test_apify_configured_does_not_trigger_missing_config():
    action = _action_for_checkpoint("Scenario 3A APIFY_TOKEN configured. readiness passed.")
    assert "Add missing Apify config" not in action
    assert "Prepare one-creator Apify sandbox call" in action


def test_apify_missing_config_triggers_missing_config():
    action = _action_for_checkpoint("Scenario 3A Apify config missing. APIFY_TOKEN not configured.")
    assert "Add missing Apify config" in action


def test_checkpoint_secret_like_value_is_blocked():
    secret_text = "OPENAI_API_KEY=sk-test1234567890abcdef1234567890"
    assert contains_checkpoint_secret(secret_text)
    assert "sk-test" not in sanitize_checkpoint_text(secret_text)
    assert not contains_checkpoint_secret("APIFY_TOKEN configured")


if __name__ == "__main__":
    test_apify_configured_does_not_trigger_missing_config()
    test_apify_missing_config_triggers_missing_config()
    test_checkpoint_secret_like_value_is_blocked()
    print("smoke checks passed")
