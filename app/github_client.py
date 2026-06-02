"""
github_client.py
Handles all GitHub API interactions.
V3: Added ai-build label to create_issue, get_merged_prs_with_diffs method.
"""

import logging
from datetime import datetime, timezone, timedelta
from github import Github, GithubException

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str, repo_name: str):
        self.gh = Github(token)
        self.repo = self.gh.get_repo(repo_name)
        self.token = token
        logger.info(f"Connected to GitHub repo: {repo_name}")

    def get_open_prs(self) -> list:
        try:
            prs = self.repo.get_pulls(state="open", base="main")
            return list(prs)
        except GithubException as e:
            logger.error(f"Failed to fetch PRs: {e}")
            return []

    def get_pr_details(self, pr) -> dict:
        try:
            files_changed = []
            for f in pr.get_files():
                files_changed.append({
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "patch": f.patch[:3000] if f.patch else ""
                })

            commit = self.repo.get_commit(pr.head.sha)
            check_runs = []
            for check in commit.get_check_runs():
                check_runs.append({
                    "name": check.name,
                    "status": check.status,
                    "conclusion": check.conclusion
                })

            return {
                "number": pr.number,
                "title": pr.title,
                "body": pr.body or "",
                "author": pr.user.login,
                "branch": pr.head.ref,
                "base": pr.base.ref,
                "files_changed": files_changed,
                "check_runs": check_runs,
                "labels": [l.name for l in pr.labels],
                "mergeable": pr.mergeable,
                "draft": pr.draft,
                "url": pr.html_url,
                "created_at": str(pr.created_at),
            }
        except GithubException as e:
            logger.error(f"Failed to get PR details for PR #{pr.number}: {e}")
            return {}

    def get_existing_bot_comment(self, pr, marker: str):
        try:
            for comment in pr.get_issue_comments():
                if marker in comment.body:
                    return comment
        except GithubException:
            pass
        return None

    def post_pr_comment(self, pr, body: str):
        try:
            pr.create_issue_comment(body)
            logger.info(f"Posted comment on PR #{pr.number}")
        except GithubException as e:
            logger.error(f"Failed to post comment on PR #{pr.number}: {e}")

    def update_pr_comment(self, comment, body: str):
        try:
            comment.edit(body)
        except GithubException as e:
            logger.error(f"Failed to update comment: {e}")

    def merge_pr(self, pr) -> bool:
        try:
            result = pr.merge(
                commit_message=f"[Control Tower] Merging PR #{pr.number}: {pr.title}",
                merge_method="squash"
            )
            logger.info(f"Merged PR #{pr.number}")
            return result.merged
        except GithubException as e:
            logger.error(f"Failed to merge PR #{pr.number}: {e}")
            return False

    def get_pr_by_number(self, number: int):
        try:
            return self.repo.get_pull(number)
        except GithubException as e:
            logger.error(f"Failed to fetch PR #{number}: {e}")
            return None

    def get_issues(self, state: str = "open") -> list:
        try:
            return list(self.repo.get_issues(state=state))
        except GithubException as e:
            logger.error(f"Failed to fetch issues: {e}")
            return []

    def get_open_pr_summaries(self, limit: int = 5) -> list[dict]:
        try:
            summaries = []
            for pr in self.get_open_prs()[:limit]:
                summaries.append({
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "draft": pr.draft,
                    "mergeable": pr.mergeable,
                    "url": pr.html_url,
                })
            return summaries
        except Exception as e:
            logger.error(f"Failed to build open PR summaries: {e}")
            return []

    def get_open_issue_summaries(self, limit: int = 5) -> list[dict]:
        try:
            summaries = []
            for issue in self.get_issues(state="open"):
                if getattr(issue, "pull_request", None):
                    continue
                summaries.append({
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "url": issue.html_url,
                })
                if len(summaries) >= limit:
                    break
            return summaries
        except Exception as e:
            logger.error(f"Failed to build open issue summaries: {e}")
            return []

    def get_latest_closed_unmerged_pr(self) -> dict | None:
        try:
            for pr in self.repo.get_pulls(state="closed", sort="updated", direction="desc"):
                if not pr.merged_at:
                    return {
                        "number": pr.number,
                        "title": pr.title,
                        "state": pr.state,
                        "url": pr.html_url,
                        "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
                    }
            return None
        except GithubException as e:
            logger.error(f"Failed to fetch closed unmerged PR: {e}")
            return None

    def create_issue(self, title: str, body: str) -> object | None:
        """
        Create a GitHub Issue and immediately add the ai-build label.
        The ai-build label triggers the GitHub Actions builder automatically.
        """
        try:
            # Ensure the ai-build label exists before applying it
            self._ensure_label("ai-build", "0075ca", "Triggers the AI builder workflow")

            issue = self.repo.create_issue(title=title, body=body)
            logger.info(f"Created GitHub Issue #{issue.number}: {title}")

            # Add ai-build label to trigger the GitHub Actions builder
            issue.add_to_labels("ai-build")
            logger.info(f"Added ai-build label to Issue #{issue.number}")

            return issue
        except GithubException as e:
            logger.error(f"Failed to create issue: {e}")
            return None

    def get_merged_prs_with_diffs(self, limit: int = 30) -> list[dict]:
        """
        Fetch recently merged PRs with file diffs.
        Used by ProjectManager for intelligent briefings.
        """
        try:
            import requests as req
            merged = []
            prs = list(self.repo.get_pulls(state="closed", sort="updated", direction="desc")[:limit])

            for pr in prs:
                if not pr.merged_at:
                    continue

                diff_text = ""
                try:
                    resp = req.get(
                        pr.url,
                        headers={
                            "Authorization": f"Bearer {self.token}",
                            "Accept": "application/vnd.github.diff",
                        },
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        diff_text = resp.text[:1500]
                except Exception as diff_err:
                    logger.warning(f"Could not fetch diff for PR #{pr.number}: {diff_err}")

                merged.append({
                    "number": pr.number,
                    "title": pr.title,
                    "body": (pr.body or "")[:300],
                    "merged_at": pr.merged_at.isoformat(),
                    "diff": diff_text,
                })

            logger.info(f"Fetched {len(merged)} merged PRs with diffs.")
            return merged
        except GithubException as e:
            logger.error(f"Failed to fetch merged PRs: {e}")
            return []

    def get_recent_activity_summary(self) -> dict:
        """
        Returns a summary of recent repo activity for stall detection.
        """
        try:
            merged_prs = self.get_merged_prs_with_diffs(limit=5)
            open_prs = self.get_open_prs()
            open_issues = self.get_issues(state="open")

            last_merge_at = None
            if merged_prs:
                last_merge_at = merged_prs[0].get("merged_at")

            days_since_merge = None
            if last_merge_at:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(last_merge_at.replace("Z", "+00:00"))
                days_since_merge = (datetime.now(timezone.utc) - last).total_seconds() / 86400

            return {
                "last_merge_at": last_merge_at,
                "days_since_last_merge": round(days_since_merge, 1) if days_since_merge else None,
                "open_prs": len(open_prs),
                "open_issues": len(open_issues),
                "is_stalled": days_since_merge is not None and days_since_merge >= 2,
            }
        except Exception as e:
            logger.error(f"Failed to get activity summary: {e}")
            return {}

    def _ensure_label(self, name: str, color: str, description: str = "") -> None:
        """Create a label if it doesn't already exist."""
        try:
            self.repo.get_label(name)
        except GithubException:
            try:
                self.repo.create_label(name=name, color=color, description=description)
                logger.info(f"Created label: {name}")
            except GithubException as e:
                logger.warning(f"Could not create label '{name}': {e}")
