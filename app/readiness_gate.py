"""
Human readiness gate messages for Control Tower PR alerts.

This module is intentionally deterministic. It only classifies text that the
caller already has and does not call OpenAI, GitHub, Railway, Airtable, Apify,
or any paid API.
"""

from __future__ import annotations


CONTEXT_RULES = (
    {
        "name": "Apify scraping",
        "keywords": ("apify", "scrap", "instagram", "tiktok", "creator profile", "kol profile"),
        "phase": "Creator scraping",
        "means": "The system is being taught how to pull creator data from Apify.",
        "manual": "Yes. Check that the Apify account, actor, dataset, and token setup are ready before approving real scraping.",
        "credentials": "Apify credential/config may be needed.",
        "cost": "Yes. Apify runs can cost money, especially repeated or large creator batches.",
        "live": "Usually no, unless the PR also changes production writebacks or live buttons.",
        "warnings": (
            "Do not approve if the Apify account or token is not ready.",
            "Do not run large real batches until a sandbox batch is proven.",
        ),
    },
    {
        "name": "OpenAI AI vetting",
        "keywords": ("openai", "gpt", "ai vet", "ai-vet", "vetting", "evaluation"),
        "phase": "AI vetting",
        "means": "The system is being taught how to judge or explain creator suitability using AI.",
        "manual": "Yes. Review whether the AI judgement matches how the team should evaluate creators.",
        "credentials": "OpenAI API setup may be needed.",
        "cost": "Yes. Each AI vetting run can spend OpenAI credits.",
        "live": "Usually no, unless connected to live campaign workflows.",
        "warnings": (
            "Do not approve if the scoring logic feels wrong for client work.",
            "Do not run repeated AI batches without watching cost.",
        ),
    },
    {
        "name": "Airtable writes",
        "keywords": ("airtable", "writeback", "write back", "update record", "create record", "patch record"),
        "phase": "Airtable writeback",
        "means": "The system may write data back into Airtable instead of only reading it.",
        "manual": "Yes. Confirm which Airtable base/table/fields are safe to update.",
        "credentials": "Airtable token/base/table setup may be needed.",
        "cost": "Usually no direct API cost, but bad writes can create cleanup work.",
        "live": "Possibly yes if it writes to the real Airtable base.",
        "warnings": (
            "Do not approve live Airtable writes without confirming the target base and fields.",
            "Do not overwrite production campaign data without a backup or sandbox proof.",
        ),
    },
    {
        "name": "Railway credential/config setup",
        "keywords": ("railway", "env", "environment variable", "secret", "credential", "config", "token"),
        "phase": "Credential/config setup",
        "means": "The system may need deployment settings or private credentials before it can work.",
        "manual": "Yes. Cheuck or an admin may need to add values in Railway manually.",
        "credentials": "Yes. Credential or environment setup is likely needed.",
        "cost": "Possibly, if deployment/runtime usage changes.",
        "live": "Possibly yes because Railway hosts the running service.",
        "warnings": (
            "Do not paste secrets into Telegram, GitHub comments, logs, or code.",
            "Do not change Railway settings unless you are intentionally doing setup.",
        ),
    },
    {
        "name": "Client report output",
        "keywords": ("client report", "report output", "client-facing", "google slides", "google sheets", "export", "presentation"),
        "phase": "Client output",
        "means": "The system may change what clients eventually see.",
        "manual": "Yes. Review the output format, wording, and numbers before approving.",
        "credentials": "Possibly, if Google file access or export setup is involved.",
        "cost": "Usually low, but generation/export services may have usage limits.",
        "live": "Possibly yes if the output goes to a real client deck or sheet.",
        "warnings": (
            "Do not approve if the report could mislead a client.",
            "Do not send generated output to clients until it has been checked by a human.",
        ),
    },
    {
        "name": "Ranking/scoring/rationale logic",
        "keywords": ("ranking", "scoring", "score", "rank", "shortlist", "recommended", "recommendation", "rationale"),
        "phase": "Business logic",
        "means": "The system may change how creators are ranked, shortlisted, or explained.",
        "manual": "Yes. Check whether the logic matches Invictus Blue's campaign judgement.",
        "credentials": "Usually no, unless AI vetting or external data is also involved.",
        "cost": "Possibly, if the logic calls AI or scraping services.",
        "live": "Possibly yes if the scores feed client output or live campaign decisions.",
        "warnings": (
            "Do not approve if the ranking rules are not explainable to the team.",
            "Do not treat the shortlist as final without human review.",
        ),
    },
    {
        "name": "Production/live switch",
        "keywords": ("production", "live switch", "go live", "live button", "make scenario", "live system", "prod"),
        "phase": "Live switch",
        "means": "The system may affect real workflows instead of a sandbox.",
        "manual": "Yes. This needs a deliberate go/no-go decision.",
        "credentials": "Possibly, depending on the connected live systems.",
        "cost": "Possibly, if live usage increases automation, scraping, or AI calls.",
        "live": "Yes. Live systems may be affected.",
        "warnings": (
            "Do not approve unless rollback is clear.",
            "Do not switch live workflow ownership without Cheuck's explicit go/no-go.",
        ),
    },
)


def build_pr_readiness_block(
    pr_title: str = "",
    decision: str = "",
    summary: str = "",
    reasoning: str = "",
    risks: list[str] | tuple[str, ...] | None = None,
    human_reason: str = "",
    hold_trigger: str = "none",
    pr_number: int | str | None = None,
) -> str:
    """Build a plain-English readiness block for a Telegram PR alert."""
    text = " ".join(
        str(value or "")
        for value in (
            pr_title,
            decision,
            summary,
            reasoning,
            human_reason,
            hold_trigger,
            " ".join(risks or []),
        )
    ).lower()

    matches = [rule for rule in CONTEXT_RULES if any(keyword in text for keyword in rule["keywords"])]
    if not matches:
        matches = [{
            "name": "General code change",
            "phase": "Build/review",
            "means": "This looks like a normal product or infrastructure change.",
            "manual": "Only if the HOLD reason above feels important.",
            "credentials": "No obvious credential setup detected.",
            "cost": "No obvious new paid usage detected.",
            "live": "No obvious live-system impact detected.",
            "warnings": (
                "Do not approve if the summary or risks are unclear.",
            ),
        }]

    primary = matches[0]
    all_names = ", ".join(rule["name"] for rule in matches)
    warning_lines = []
    seen_warnings = set()
    for rule in matches:
        for warning in rule["warnings"]:
            if warning not in seen_warnings:
                warning_lines.append(f"- {warning}")
                seen_warnings.add(warning)
            if len(warning_lines) >= 3:
                break
        if len(warning_lines) >= 3:
            break

    if str(decision).upper() == "HOLD" or human_reason:
        next_command = f"`approve {pr_number}` only if you are comfortable with the warnings above."
    elif str(decision).upper() in {"FIX"}:
        next_command = f"`details {pr_number}` if you want to inspect the PR before the builder fixes it."
    else:
        next_command = f"`details {pr_number}` if you want to inspect it, or `skip {pr_number}` to leave it alone."

    return (
        "\n\n*Human readiness gate*\n"
        f"*Phase/context:* {primary['phase']}\n"
        f"*Detected area:* {all_names}\n"
        f"*Plain English:* {primary['means']}\n"
        f"*Do you need to do anything manually?* {primary['manual']}\n"
        f"*Credential/API setup needed?* {primary['credentials']}\n"
        f"*Cost involved?* {primary['cost']}\n"
        f"*Live systems affected?* {primary['live']}\n"
        f"*Next Telegram command:* {next_command}\n"
        "*What not to do:*\n"
        + "\n".join(warning_lines)
    )