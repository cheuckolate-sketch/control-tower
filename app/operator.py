"""Deterministic operator summaries for Telegram."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NOT_VERIFIED = "Not verified"


def _fmt_pr(pr: dict[str, Any]) -> str:
    number = pr.get("number", "?")
    title = pr.get("title") or "Untitled"
    state = pr.get("state") or pr.get("status") or ""
    extra = f" [{state}]" if state else ""
    return f"#{number} {title}{extra}"


def _join(items: list[str], empty: str = "None") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def classify_lane(text: str) -> dict[str, str]:
    lowered = text.lower()
    risky_terms = (
        "apify", "openai", "paid", "railway", "secret", "env", "credential",
        "live", "make", "airtable", "production", "writeback", "client",
        "ranking", "scoring", "rationale", "shortlist", "delete", "rename",
        "destructive", "low confidence",
    )
    if any(term in lowered for term in risky_terms):
        return {
            "lane": "NEEDS CHEUCK",
            "why": "The next step may involve cost, credentials, live systems, or business/client logic.",
            "cheuck_needed": "Yes",
        }
    if "pr" in lowered or "review" in lowered:
        return {
            "lane": "AUTO-REVIEW",
            "why": "Tower can review and recommend, but approval still controls merge decisions.",
            "cheuck_needed": "No, unless the review flags a HOLD.",
        }
    if "issue" in lowered or "brief" in lowered or "prepare" in lowered:
        return {
            "lane": "AUTO-PREPARE",
            "why": "Tower can prepare a safe draft or recommendation without changing live systems.",
            "cheuck_needed": "No, unless kickoff is requested.",
        }
    return {
        "lane": "AUTO-INFORM",
        "why": "This is status reporting only.",
        "cheuck_needed": "No",
    }


def build_next_best_action(snapshot: dict[str, Any]) -> str:
    checkpoint = snapshot.get("runtime_checkpoint") or {}
    checkpoint_text = checkpoint.get("text") or ""
    latest_merged = snapshot.get("latest_merged_prs") or []
    open_prs = snapshot.get("open_prs") or []

    if "APIFY_TOKEN" in checkpoint_text or "apify" in checkpoint_text.lower() and "missing" in checkpoint_text.lower():
        action = "Add missing Apify config in Railway, then rerun the readiness endpoint."
        why = "The latest runtime checkpoint says Scenario 3A readiness is blocked by missing Apify config."
        command = "checkpoint Scenario 3A readiness rechecked. Record the endpoint version and missing config only."
        do_not = "Do not paste secrets into Telegram, GitHub, docs, or ChatGPT. Do not run Apify scraping yet."
        manual = "Yes, Railway env only"
        cost = "None for readiness check; paid risk starts at sandbox scraping"
        live = "None"
    elif open_prs:
        pr = open_prs[0]
        action = f"Review PR #{pr.get('number')} and decide approve, reject, details, or skip."
        why = "There is an open PR waiting for Tower/Cheuck decision."
        command = f"details {pr.get('number')}"
        do_not = "Do not approve if checks are failing, missing, or scope does not match the issue."
        manual = "No, unless the PR requires HOLD review"
        cost = "Low unless the PR changes paid API behavior"
        live = "Low unless the PR touches live systems"
    elif latest_merged:
        pr = latest_merged[0]
        action = f"Verify runtime after merged PR #{pr.get('number')} if deployment status matters."
        why = "GitHub can confirm merge state, but not live runtime behavior."
        command = "checkpoint <runtime fact>"
        do_not = "Do not assume deployment or endpoint behavior without a runtime checkpoint."
        manual = "Yes, if live/runtime confirmation is needed"
        cost = "None"
        live = "Not verified"
    else:
        action = "Ask for current project status."
        why = "No verified PR or runtime checkpoint is available in Tower state."
        command = "what's next"
        do_not = "Do not create duplicate issues without checking existing work."
        manual = "No"
        cost = "None"
        live = "Not verified"

    lane = classify_lane(action + " " + why + " " + manual + " " + cost + " " + live)
    return (
        "*Next Best Action:*\n"
        f"- Action: {action}\n"
        f"- Why now: {why}\n"
        f"- Autopilot lane: {lane['lane']}\n"
        f"- Cheuck needed: {lane['cheuck_needed']}\n"
        f"- Manual setup needed: {manual}\n"
        f"- Cost risk: {cost}\n"
        f"- Live risk: {live}\n"
        f"- Exact command: `{command}`\n"
        f"- Do not do: {do_not}"
    )


def build_operator_snapshot(
    repo_name: str,
    phase: dict[str, Any] | None,
    latest_merged_prs: list[dict[str, Any]],
    open_prs: list[dict[str, Any]],
    open_issues: list[dict[str, Any]],
    latest_closed_unmerged_pr: dict[str, Any] | None,
    runtime_checkpoint: dict[str, Any] | None,
    known_blocker: str = NOT_VERIFIED,
) -> str:
    phase_text = NOT_VERIFIED
    if phase:
        phase_text = f"Phase {phase.get('id')}: {phase.get('name')} [{phase.get('status')}]"

    checkpoint_text = "Runtime checkpoint: Not recorded."
    if runtime_checkpoint and runtime_checkpoint.get("text"):
        checkpoint_text = f"Runtime checkpoint: {runtime_checkpoint['text']}"

    snapshot = {
        "latest_merged_prs": latest_merged_prs,
        "open_prs": open_prs,
        "runtime_checkpoint": runtime_checkpoint or {},
    }

    return (
        "*Operator Snapshot:*\n"
        f"- Repo watched: `{repo_name}`\n"
        f"- Active phase: {phase_text}\n"
        f"- Latest merged PRs:\n{_join([_fmt_pr(pr) for pr in latest_merged_prs[:3]], NOT_VERIFIED)}\n"
        f"- Open PRs:\n{_join([_fmt_pr(pr) for pr in open_prs[:5]], 'None')}\n"
        f"- Relevant open issues:\n{_join([_fmt_pr(issue) for issue in open_issues[:5]], 'None')}\n"
        f"- Latest closed unmerged PR: {_fmt_pr(latest_closed_unmerged_pr) if latest_closed_unmerged_pr else NOT_VERIFIED}\n"
        f"- Known blocker: {known_blocker}\n"
        f"- {checkpoint_text}\n"
        "- Deployment status: Not verified.\n"
        "- Live runtime state: Not verified.\n"
        "- Credential/API setup needed: Not verified.\n"
        "- Cost risk: Low for status checks; higher if next step involves Apify/OpenAI.\n"
        "- Live-system risk: Not verified.\n\n"
        f"{build_next_best_action(snapshot)}"
    )


def build_operator_action_card(
    pr_number: int | str,
    pr_title: str,
    decision: str,
    files: list[dict[str, Any]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    risks: list[str] | None = None,
    hold_trigger: str = "none",
) -> str:
    files = files or []
    checks = checks or []
    risks = risks or []
    file_names = [f.get("filename", "unknown") for f in files[:5]]
    check_lines = [
        f"{c.get('name')}: {c.get('conclusion') or c.get('status') or 'unknown'}"
        for c in checks[:5]
    ]
    risk_text = "; ".join(risks[:3]) if risks else "No specific risks listed."
    cost_risk = "Medium/High" if hold_trigger == "cost" or "cost" in risk_text.lower() else "Low/Not flagged"
    live_risk = "Medium/High" if hold_trigger == "live_system" or "live" in risk_text.lower() else "Low/Not flagged"
    secrets_risk = "Flagged" if any("secret" in item.lower() or "token" in item.lower() for item in risks) else "Not flagged"
    command = f"details {pr_number}" if decision in {"FIX", "HOLD"} else f"approve {pr_number}"

    return (
        "\n\n*Operator Action Card:*\n"
        f"- PR: #{pr_number} {pr_title}\n"
        f"- Decision/status: {decision}\n"
        f"- Changed files: {', '.join(file_names) if file_names else NOT_VERIFIED}\n"
        f"- Checks: {'; '.join(check_lines) if check_lines else NOT_VERIFIED}\n"
        f"- Scope/risk summary: {risk_text}\n"
        f"- Cost risk: {cost_risk}\n"
        f"- Live-system impact: {live_risk}\n"
        f"- Secrets risk: {secrets_risk}\n"
        f"- Manual action needed: {'Yes' if decision in {'HOLD', 'MERGE'} else 'No unless you want to inspect'}\n"
        f"- Recommended command: `{command}`\n"
        "- Do-not-do: Do not approve if checks are failing, secrets are involved, or scope does not match the issue."
    )


def checkpoint_is_stale(checkpoint: dict[str, Any] | None, latest_merged_prs: list[dict[str, Any]]) -> bool:
    if not checkpoint or not checkpoint.get("timestamp") or not latest_merged_prs:
        return False
    latest = latest_merged_prs[0].get("merged_at")
    if not latest:
        return False
    try:
        checkpoint_dt = datetime.fromisoformat(checkpoint["timestamp"].replace("Z", "+00:00"))
        merged_dt = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
        if checkpoint_dt.tzinfo is None:
            checkpoint_dt = checkpoint_dt.replace(tzinfo=timezone.utc)
        if merged_dt.tzinfo is None:
            merged_dt = merged_dt.replace(tzinfo=timezone.utc)
        return checkpoint_dt < merged_dt
    except Exception:
        return False
