"""
readiness_gate.py
Control Tower V3.2 - human-readable readiness guidance.

This module translates technical PR risk signals into plain-English operator guidance
for Cheuck. It is deterministic on purpose. Do not put secrets here.
"""

from __future__ import annotations


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def build_pr_readiness_block(
    *,
    pr_number: int | str | None = None,
    pr_title: str = "",
    summary: str = "",
    reasoning: str = "",
    risks: list[str] | None = None,
    human_reason: str = "",
    hold_trigger: str = "none",
    decision: str = "",
) -> str:
    """
    Build a Telegram-safe Markdown block that tells Cheuck what human action is needed.

    Inputs are intentionally simple because this block is used after the AI reviewer.
    It does not call OpenAI, GitHub, Railway, Airtable, Apify, or any paid API.
    """
    risk_text = " ".join(risks or [])
    context = " ".join([
        pr_title or "",
        summary or "",
        reasoning or "",
        risk_text,
        human_reason or "",
        hold_trigger or "",
        decision or "",
    ]).lower()

    is_apify = _contains_any(context, [
        "apify", "scrap", "scraping", "instagram", "tiktok", "actor", "dataset",
    ])
    is_openai = _contains_any(context, [
        "openai", "gpt", "ai vetting", "vetting", "ai evaluation", "prompt",
    ])
    is_airtable = _contains_any(context, [
        "airtable", "writeback", "write-back", "record", "field", "schema",
    ])
    is_railway_or_secret = _contains_any(context, [
        "railway", "env", "environment", "secret", "token", "api key", "key",
    ])
    is_client_output = hold_trigger == "client_output" or _contains_any(context, [
        "client report", "client-facing", "report output", "google sheet", "export",
    ])
    is_business_logic = _contains_any(context, [
        "score", "scoring", "rank", "ranking", "rationale", "shortlist",
        "recommendation", "selection", "competitor", "brand safety", "audience fit",
    ])
    is_live_system = hold_trigger == "live_system" or _contains_any(context, [
        "live", "production", "make", "button", "delete", "rename", "destructive",
    ])
    is_cost = hold_trigger == "cost" or is_apify or is_openai

    if not any([
        hold_trigger != "none",
        decision in {"HOLD", "MERGE", "FIX"},
        is_apify,
        is_openai,
        is_airtable,
        is_railway_or_secret,
        is_client_output,
        is_business_logic,
        is_live_system,
    ]):
        return ""

    if decision == "FIX":
        action_needed = "Yes. This should go back to the builder before merging."
    elif decision == "HOLD":
        action_needed = "Yes. This needs human judgment before merge."
    elif decision == "MERGE":
        action_needed = "Yes. Review once, then approve only if the details make sense."
    else:
        action_needed = "Probably not, unless the PR details look suspicious."

    manual_setup = "No manual setup yet."
    if is_railway_or_secret:
        manual_setup = "Maybe. If a key or env var is needed, add it in Railway only."
    elif is_apify:
        manual_setup = "Not yet, unless this PR is specifically the Apify readiness-check step."
    elif is_openai:
        manual_setup = "Not yet, unless this PR is specifically the OpenAI readiness-check step."

    api_key_needed = "No."
    if is_apify:
        api_key_needed = "Not in Telegram or GitHub. `APIFY_TOKEN` belongs in Railway only, and only when the readiness-check step asks for it."
    elif is_openai:
        api_key_needed = "Not in Telegram or GitHub. `OPENAI_API_KEY` belongs in Railway only, and only when the readiness-check step asks for it."
    elif is_railway_or_secret:
        api_key_needed = "Maybe. Use Railway variables only. Never paste secrets into Telegram, GitHub issues, PRs, or ChatGPT."

    cost_risk = "No obvious paid API risk."
    if is_apify and is_openai:
        cost_risk = "Yes. Apify and OpenAI can both spend money. Confirm this is sandbox-limited before approving."
    elif is_apify:
        cost_risk = "Yes once live scraping is called. Planner/readiness-only changes should not spend money."
    elif is_openai:
        cost_risk = "Yes once real OpenAI calls are enabled. Prompt/planning-only changes should not spend money."
    elif hold_trigger == "cost":
        cost_risk = "Yes. Cost trigger was detected."

    live_risk = "No obvious live-system change."
    if is_live_system:
        live_risk = "Yes or possible. Do not approve unless sandbox impact and rollback are clear."
    elif is_airtable:
        live_risk = "Possible. Confirm sandbox-only before approving any Airtable writeback."

    business_risk = "Low."
    if is_business_logic and is_client_output:
        business_risk = "High. This may affect scoring, ranking, rationale, and what clients see."
    elif is_business_logic:
        business_risk = "High. This may change KOL scoring, ranking, shortlist, or recommendation logic."
    elif is_client_output:
        business_risk = "High. This may change client-facing output."

    next_command = "Check details before deciding."
    if pr_number:
        if decision == "FIX":
            next_command = f"`details {pr_number}` first, then likely `reject {pr_number}` if the fix is not already clear."
        elif decision == "HOLD":
            next_command = f"`details {pr_number}` first. Approve only after the risk is clear."
        else:
            next_command = f"`details {pr_number}` if unsure."

    do_not = [
        "Do not paste API keys into Telegram, GitHub, PR comments, Codex, or ChatGPT.",
    ]
    if is_apify:
        do_not.append("Do not approve live Apify scraping until planner/readiness and sandbox proof exist.")
    if is_openai:
        do_not.append("Do not approve real OpenAI calls unless sandbox limits and expected cost are clear.")
    if is_business_logic:
        do_not.append("Do not approve scoring/ranking/rationale changes without sample outputs.")
    if is_client_output:
        do_not.append("Do not approve client-facing report changes without checking the output shape.")
    if is_airtable or is_live_system:
        do_not.append("Do not approve production writebacks or live button switches without explicit approval.")

    do_not_text = "\n".join([f"• {item}" for item in do_not[:5]])

    return (
        "\n\n🧭 *Layman next action*\n"
        f"*Cheuck action needed:* {action_needed}\n"
        f"*Manual setup needed:* {manual_setup}\n"
        f"*API key needed:* {api_key_needed}\n"
        f"*Cost risk:* {cost_risk}\n"
        f"*Live-system risk:* {live_risk}\n"
        f"*Business-logic risk:* {business_risk}\n"
        f"*Next safe command:* {next_command}\n"
        f"*Do not:*\n{do_not_text}"
    )
