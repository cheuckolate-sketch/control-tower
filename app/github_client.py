"""
github_client.py
Handles all GitHub API interactions.
V2: Added create_issue method for planner kickoff.
"""

import logging
from github import Github, GithubException

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str, repo_name: str):
        self.gh = Github(token)
        self.repo = self.gh.get_repo(repo_name)
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

    def create_issue(self, title: str, body: str):
        """Create a new GitHub Issue for the next milestone."""
        try:
            issue = self.repo.create_issue(title=title, body=body)
            logger.info(f"Created GitHub Issue #{issue.number}: {title}")
            return issue
        except GithubException as e:
            logger.error(f"Failed to create issue: {e}")
            return None
