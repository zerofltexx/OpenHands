"""Azure DevOps data collector for tracking resolver interactions."""

import os
from datetime import datetime

from integrations.models import Message
from integrations.types import PRStatus
from storage.openhands_pr import OpenhandsPR
from storage.openhands_pr_store import OpenhandsPRStore

from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.utils.logger import openhands_logger as logger

COLLECT_AZURE_DEVOPS_INTERACTIONS = os.getenv(
    'COLLECT_AZURE_DEVOPS_INTERACTIONS', ''
).lower() in ('true', '1', 'yes')


class AzureDevOpsDataCollector:
    """Records completed Azure DevOps PRs into the OpenhandsPR table.

    Only completed/abandoned PRs are tracked today. Per-conversation interaction
    snapshots and full-PR enrichment (peer GithubDataCollector helpers) are not
    yet implemented for Azure DevOps because the webhook payload does not carry
    enough context (no organization on `OpenhandsPR`) and there is no caller
    that needs it.
    """

    def __init__(self, access_token: str | None = None):
        self.access_token = access_token

    def process_payload(self, message: Message):
        """Track PR completed events from Azure DevOps webhook payloads."""
        if not COLLECT_AZURE_DEVOPS_INTERACTIONS:
            return

        try:
            raw_payload = message.message if isinstance(message.message, dict) else {}

            if self._is_pr_completed(raw_payload):
                self._track_completed_pr(raw_payload)

        except Exception as e:
            logger.exception(f'[Azure DevOps]: Error processing payload: {e}')

    def _is_pr_completed(self, payload: dict) -> bool:
        """True iff the payload is a `git.pullrequest.updated` for a completed/abandoned PR."""
        if payload.get('eventType') != 'git.pullrequest.updated':
            return False
        status = payload.get('resource', {}).get('status', '').lower()
        return status in ('abandoned', 'completed')

    def _track_completed_pr(self, payload: dict):
        """Record a completed/abandoned PR into the OpenhandsPR store."""
        try:
            resource = payload.get('resource', {})
            repository = resource.get('repository', {})
            project = repository.get('project', {})

            repo_id = repository.get('id', '')
            repo_name = repository.get('name', '')
            pr_number = resource.get('pullRequestId', 0)
            project_name = project.get('name', '')
            status = resource.get('status', '').lower()

            if status == 'completed' and resource.get('mergeStatus') == 'succeeded':
                pr_status = PRStatus.MERGED
                merged = True
            else:
                pr_status = PRStatus.CLOSED
                merged = False

            created_at = resource.get('creationDate')
            closed_date = resource.get('closedDate')
            closed_at = (
                datetime.fromisoformat(closed_date.replace('Z', '+00:00'))
                if closed_date
                else datetime.now()
            )

            # Note: webhook payload does not include diff metrics; counts stay at 0.
            pr = OpenhandsPR(
                repo_name=f'{project_name}/{repo_name}',
                repo_id=repo_id,
                pr_number=pr_number,
                status=pr_status,
                provider=ProviderType.AZURE_DEVOPS.value,
                installation_id='',  # Azure DevOps doesn't have installation IDs
                private=True,  # Assume private for Azure DevOps
                num_reviewers=0,
                num_commits=0,
                num_review_comments=0,
                num_changed_files=0,
                num_additions=0,
                num_deletions=0,
                merged=merged,
                created_at=created_at,
                closed_at=closed_at,
                openhands_helped_author=None,
                num_openhands_commits=None,
                num_openhands_review_comments=None,
                num_general_comments=0,
            )

            OpenhandsPRStore.get_instance().insert_pr(pr)
            logger.info(
                f'[Azure DevOps]: Tracked PR {pr_status}: {project_name}/{repo_name}#{pr_number}'
            )

        except Exception as e:
            logger.exception(f'[Azure DevOps]: Error tracking completed PR: {e}')
