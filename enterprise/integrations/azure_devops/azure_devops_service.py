import asyncio

from integrations.utils import AZURE_DEVOPS_WEBHOOK_URL, store_repositories_in_db
from pydantic import SecretStr
from server.auth.token_manager import TokenManager
from storage.azure_devops_webhook import AzureDevOpsWebhook, WebhookStatus
from storage.azure_devops_webhook_store import AzureDevOpsWebhookStore

from openhands.core.logger import openhands_logger as logger
from openhands.integrations.azure_devops.azure_devops_service import (
    AzureDevOpsServiceImpl as BaseAzureDevOpsService,
)
from openhands.integrations.service_types import (
    ProviderType,
    RateLimitError,
    Repository,
    RequestMethod,
)
from openhands.server.types import AppMode


class SaaSAzureDevOpsService(BaseAzureDevOpsService):
    """SaaS-specific Azure DevOps service with webhook automation support.

    This extends the base AzureDevOpsServiceImpl with:
    - Token management via Keycloak/external auth
    - Webhook (Service Hook) installation automation
    - Repository data storage in database
    """

    def __init__(
        self,
        user_id: str | None = None,
        external_auth_token: SecretStr | None = None,
        external_auth_id: str | None = None,
        token: SecretStr | None = None,
        external_token_manager: bool = False,
        base_domain: str | None = None,
    ):
        logger.info(
            f'SaaSAzureDevOpsService created with user_id {user_id}, '
            f'external_auth_id {external_auth_id}, '
            f"external_auth_token {'set' if external_auth_token else 'None'}, "
            f"token {'set' if token else 'None'}, "
            f'external_token_manager {external_token_manager}'
        )
        super().__init__(
            user_id=user_id,
            external_auth_token=external_auth_token,
            external_auth_id=external_auth_id,
            token=token,
            external_token_manager=external_token_manager,
            base_domain=base_domain,
        )

        self.external_auth_token = external_auth_token
        self.external_auth_id = external_auth_id
        self.token_manager = TokenManager(external=external_token_manager)

    async def get_latest_token(self) -> SecretStr | None:
        """Get the latest Azure DevOps token from the token manager."""
        azure_devops_token = None

        if self.external_auth_token:
            azure_devops_token = SecretStr(
                await self.token_manager.get_idp_token(
                    self.external_auth_token.get_secret_value(),
                    idp=ProviderType.AZURE_DEVOPS,
                )
            )
            logger.debug(
                f'Got Azure DevOps token from access token: {self.external_auth_token}'
            )
        elif self.external_auth_id:
            offline_token = await self.token_manager.load_offline_token(
                self.external_auth_id
            )
            azure_devops_token = SecretStr(
                await self.token_manager.get_idp_token_from_offline_token(
                    offline_token, ProviderType.AZURE_DEVOPS
                )
            )
            logger.info(
                f'Got Azure DevOps token from external auth user ID: {self.external_auth_id}'
            )
        elif self.user_id:
            azure_devops_token = SecretStr(
                await self.token_manager.get_idp_token_from_idp_user_id(
                    self.user_id, ProviderType.AZURE_DEVOPS
                )
            )
            logger.debug(f'Got Azure DevOps token from user ID: {self.user_id}')
        else:
            logger.warning('external_auth_token and user_id not set!')

        return azure_devops_token

    async def add_repositories_to_webhook_db(
        self, repositories: list[dict]
    ) -> None:
        """Add repositories to the database for webhook tracking.

        Args:
            repositories: List of repository dicts from Azure DevOps API
        """
        webhooks = []

        for repo in repositories:
            # Parse repository info
            # Repository object contains: id, name, project.name, project.id
            project = repo.get('project', {})
            project_name = project.get('name', '')

            webhook = AzureDevOpsWebhook(
                organization=self.organization,
                project=project_name,
                repository_id=repo.get('id', ''),
                user_id=self.external_auth_id or self.user_id or '',
                webhook_exists=False,
                status=WebhookStatus.PENDING,
            )
            webhooks.append(webhook)

        # Store webhooks in the database
        if webhooks:
            try:
                webhook_store = AzureDevOpsWebhookStore()
                await webhook_store.store_webhooks(webhooks)
                logger.info(
                    f'Added Azure DevOps webhooks to db for user {self.external_auth_id}'
                )
            except Exception:
                logger.warning(
                    'Failed to add Azure DevOps webhooks to db', exc_info=True
                )

    async def store_repository_data(
        self, raw_repositories: list[dict], repositories: list[Repository]
    ) -> None:
        """Store repository data in the database.

        Args:
            raw_repositories: Raw repository dicts from Azure DevOps API
            repositories: List of Repository objects to store
        """
        try:
            # Add repositories to webhook tracking database
            await self.add_repositories_to_webhook_db(raw_repositories)

            # Store repositories in the general repository database
            await store_repositories_in_db(
                repositories, self.external_auth_id or self.user_id or ''
            )

            logger.info(
                f'Successfully stored repository data for user {self.external_auth_id}'
            )
        except Exception:
            logger.warning('Error storing repository data', exc_info=True)

    async def get_all_repositories(
        self, sort: str, app_mode: AppMode, store_in_background: bool = True
    ) -> list[Repository]:
        """Get repositories for the authenticated user.

        Overrides the base implementation to also store webhook tracking data.

        Args:
            sort: The field to sort repositories by
            app_mode: The application mode (OSS or SAAS)
            store_in_background: Whether to store data in background task

        Returns:
            List of Repository objects
        """
        # Get repositories from all accessible projects
        all_repos: list[dict] = []
        repositories: list[Repository] = []

        try:
            # First, get all projects the user has access to
            projects_url = f'{self.base_url}/_apis/projects?api-version=7.0'
            projects_response, _ = await self._make_request(projects_url)
            projects = projects_response.get('value', [])

            # Then get repositories from each project
            for project in projects:
                project_name = project.get('name', '')
                repos_url = f'{self.base_url}/{project_name}/_apis/git/repositories?api-version=7.0'

                try:
                    repos_response, _ = await self._make_request(repos_url)
                    project_repos = repos_response.get('value', [])
                    all_repos.extend(project_repos)
                except Exception:
                    logger.warning(
                        f'Error fetching repositories for project {project_name}',
                        exc_info=True,
                    )
                    continue

            # Convert to Repository objects
            for repo in all_repos:
                project = repo.get('project', {})
                project_name = project.get('name', '')
                repo_name = repo.get('name', '')
                full_name = f'{self.organization}/{project_name}/{repo_name}'

                repositories.append(
                    Repository(
                        id=repo.get('id', ''),
                        full_name=full_name,
                        git_provider=ProviderType.AZURE_DEVOPS,
                        is_public=False,  # Azure DevOps repos are private by default
                    )
                )

            # Store webhook and repository info
            if store_in_background:
                asyncio.create_task(
                    self.store_repository_data(all_repos, repositories)
                )
            else:
                await self.store_repository_data(all_repos, repositories)

        except Exception:
            logger.warning('Error fetching all repositories', exc_info=True)

        return repositories

    async def check_resource_exists(
        self, organization: str, project: str, repository_id: str
    ) -> tuple[bool, WebhookStatus | None]:
        """Check if the repository exists and the user has access to it.

        Args:
            organization: Azure DevOps organization
            project: Project name
            repository_id: Repository ID or name

        Returns:
            tuple[bool, WebhookStatus | None]: (exists, status)
        """
        url = f'https://dev.azure.com/{organization}/{project}/_apis/git/repositories/{repository_id}?api-version=7.0'

        try:
            response, _ = await self._make_request(url)
            return bool(response and 'id' in response), None
        except RateLimitError:
            return False, WebhookStatus.RATE_LIMITED
        except Exception:
            logger.warning('Resource existence check failed', exc_info=True)
            return False, WebhookStatus.INVALID

    async def check_webhook_exists_on_resource(
        self,
        organization: str,
        project: str,
        webhook_url: str,
    ) -> tuple[bool, WebhookStatus | None]:
        """Check if a webhook (service hook subscription) already exists.

        Args:
            organization: Azure DevOps organization
            project: Project name
            webhook_url: The URL of the webhook to check for

        Returns:
            tuple[bool, WebhookStatus | None]: (exists, status)
        """
        # Get all service hook subscriptions for the project
        url = f'https://dev.azure.com/{organization}/_apis/hooks/subscriptions?api-version=7.0'

        try:
            response, _ = await self._make_request(url)
            subscriptions = response.get('value', [])

            # Check if any subscription has the specified URL
            for subscription in subscriptions:
                consumer_inputs = subscription.get('consumerInputs', {})
                if consumer_inputs.get('url') == webhook_url:
                    return True, None

            return False, None

        except RateLimitError:
            return False, WebhookStatus.RATE_LIMITED
        except Exception:
            logger.warning('Webhook existence check failed', exc_info=True)
            return False, WebhookStatus.INVALID

    async def check_user_has_admin_access_to_resource(
        self, organization: str, project: str
    ) -> tuple[bool, WebhookStatus | None]:
        """Check if the user has admin access to the project.

        In Azure DevOps, service hooks require Project Administrator permissions.

        Args:
            organization: Azure DevOps organization
            project: Project name

        Returns:
            tuple[bool, WebhookStatus | None]: (has_access, status)
        """
        # Check if user can access project settings (requires admin)
        # We'll try to list service hooks - if successful, user has access
        url = f'https://dev.azure.com/{organization}/_apis/hooks/subscriptions?api-version=7.0'

        try:
            response, _ = await self._make_request(url)
            # If we get a response without error, user has sufficient permissions
            return 'value' in response, None
        except RateLimitError:
            return False, WebhookStatus.RATE_LIMITED
        except Exception:
            # Permission denied or other error
            logger.warning('Admin access check failed', exc_info=True)
            return False, WebhookStatus.INVALID

    async def install_webhook(
        self,
        organization: str,
        project: str,
        repository_id: str,
        webhook_url: str,
        webhook_secret: str,
        webhook_uuid: str,
        event_types: list[str] | None = None,
    ) -> tuple[str | None, WebhookStatus | None]:
        """Install a webhook (service hook subscription) for a repository.

        Creates an Azure DevOps Service Hook subscription that sends events
        to the specified webhook URL.

        Args:
            organization: Azure DevOps organization
            project: Project name
            repository_id: Repository ID
            webhook_url: URL to receive webhook events
            webhook_secret: Secret for HMAC verification (sent in header)
            webhook_uuid: Unique identifier for this webhook installation
            event_types: List of event types to subscribe to

        Returns:
            tuple[str | None, WebhookStatus | None]: (subscription_id, status)
        """
        if event_types is None:
            # Default to code push and PR events
            event_types = [
                'git.push',
                'git.pullrequest.created',
                'git.pullrequest.updated',
                'git.pullrequest.merged',
            ]

        # Get project ID (required for service hook)
        project_url = f'https://dev.azure.com/{organization}/_apis/projects/{project}?api-version=7.0'

        try:
            project_response, _ = await self._make_request(project_url)
            project_id = project_response.get('id')

            if not project_id:
                logger.warning(f'Could not get project ID for {project}')
                return None, WebhookStatus.INVALID

        except Exception:
            logger.warning('Failed to get project ID', exc_info=True)
            return None, WebhookStatus.INVALID

        # Create service hook subscriptions for each event type
        subscription_ids = []
        url = f'https://dev.azure.com/{organization}/_apis/hooks/subscriptions?api-version=7.0'

        for event_type in event_types:
            subscription_data = {
                'publisherId': 'tfs',
                'eventType': event_type,
                'resourceVersion': '1.0',
                'consumerId': 'webHooks',
                'consumerActionId': 'httpRequest',
                'publisherInputs': {
                    'projectId': project_id,
                    'repository': repository_id,
                },
                'consumerInputs': {
                    'url': webhook_url,
                    'httpHeaders': (
                        f'X-OpenHands-User-ID:{self.external_auth_id or self.user_id or ""}\n'
                        f'X-OpenHands-Webhook-ID:{webhook_uuid}\n'
                        f'X-OpenHands-Webhook-Secret:{webhook_secret}'
                    ),
                },
            }

            try:
                response, _ = await self._make_request(
                    url=url, params=subscription_data, method=RequestMethod.POST
                )

                if response and 'id' in response:
                    subscription_ids.append(response['id'])
                    logger.info(
                        f'Created service hook subscription for {event_type}: {response["id"]}'
                    )

            except RateLimitError:
                return None, WebhookStatus.RATE_LIMITED
            except Exception:
                logger.warning(
                    f'Failed to create subscription for {event_type}', exc_info=True
                )
                continue

        if subscription_ids:
            # Return the first subscription ID as the main identifier
            return subscription_ids[0], None

        return None, WebhookStatus.INVALID

    async def delete_webhook(
        self, organization: str, subscription_id: str
    ) -> tuple[bool, WebhookStatus | None]:
        """Delete a webhook (service hook subscription).

        Args:
            organization: Azure DevOps organization
            subscription_id: The subscription ID to delete

        Returns:
            tuple[bool, WebhookStatus | None]: (success, status)
        """
        url = f'https://dev.azure.com/{organization}/_apis/hooks/subscriptions/{subscription_id}?api-version=7.0'

        try:
            await self._make_request(url=url, method=RequestMethod.DELETE)
            return True, None
        except RateLimitError:
            return False, WebhookStatus.RATE_LIMITED
        except Exception:
            logger.warning('Failed to delete webhook subscription', exc_info=True)
            return False, WebhookStatus.INVALID

    async def user_has_write_access(self, repository: str) -> bool:
        """Check if the user has write access to the repository.

        Args:
            repository: Repository in format organization/project/repo

        Returns:
            bool: True if user has write access
        """
        try:
            org, project, repo = self._parse_repository(repository)
            url = f'https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}?api-version=7.0'

            response, _ = await self._make_request(url)

            # Check if we can get the repository - if so, we have at least read access
            # For write access, we'd need to check permissions more specifically
            # For now, assume if we can read, we can contribute
            return bool(response and 'id' in response)

        except Exception:
            logger.warning('Access check failed', exc_info=True)
            return False
