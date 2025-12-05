import asyncio
from typing import cast
from uuid import uuid4

from integrations.utils import AZURE_DEVOPS_WEBHOOK_URL
from storage.azure_devops_webhook import AzureDevOpsWebhook, WebhookStatus
from storage.azure_devops_webhook_store import AzureDevOpsWebhookStore

from openhands.core.logger import openhands_logger as logger
from openhands.integrations.azure_devops.azure_devops_service import (
    AzureDevOpsServiceImpl,
)
from openhands.integrations.service_types import GitService

CHUNK_SIZE = 100

# Event types to subscribe to for Azure DevOps webhooks
EVENT_TYPES: list[str] = [
    'git.push',
    'git.pullrequest.created',
    'git.pullrequest.updated',
    'git.pullrequest.merged',
]


class BreakLoopException(Exception):
    """Exception to break out of the processing loop for a single webhook."""

    pass


class VerifyWebhookStatus:
    """Background job to verify and install Azure DevOps webhooks.

    This class handles the process of:
    1. Fetching webhooks that need processing (webhook_exists=False)
    2. Verifying conditions (resource exists, user has admin access, webhook not already installed)
    3. Installing webhooks when conditions are met
    """

    async def fetch_rows(
        self, webhook_store: AzureDevOpsWebhookStore
    ) -> list[AzureDevOpsWebhook]:
        """Fetch webhooks that need processing."""
        webhooks = await webhook_store.filter_rows(limit=CHUNK_SIZE)
        return webhooks

    def determine_if_rate_limited(self, status: WebhookStatus | None) -> None:
        """Check if we hit a rate limit and should stop processing."""
        if status == WebhookStatus.RATE_LIMITED:
            raise BreakLoopException()

    async def check_if_resource_exists(
        self,
        azure_devops_service: GitService,
        webhook: AzureDevOpsWebhook,
        webhook_store: AzureDevOpsWebhookStore,
    ) -> None:
        """Check if the Azure DevOps repository still exists."""
        from integrations.azure_devops.azure_devops_service import (
            SaaSAzureDevOpsService,
        )

        service = cast(SaaSAzureDevOpsService, azure_devops_service)

        does_resource_exist, status = await service.check_resource_exists(
            organization=webhook.organization,
            project=webhook.project,
            repository_id=webhook.repository_id,
        )

        logger.info(
            'Does resource exist',
            extra={
                'does_resource_exist': does_resource_exist,
                'status': status,
                'organization': webhook.organization,
                'project': webhook.project,
                'repository_id': webhook.repository_id,
            },
        )

        self.determine_if_rate_limited(status)

        if not does_resource_exist and status != WebhookStatus.RATE_LIMITED:
            await webhook_store.delete_webhook(webhook)
            raise BreakLoopException()

    async def check_if_user_has_admin_access_to_resource(
        self,
        azure_devops_service: GitService,
        webhook: AzureDevOpsWebhook,
        webhook_store: AzureDevOpsWebhookStore,
    ) -> None:
        """Check if the user still has admin access to the project."""
        from integrations.azure_devops.azure_devops_service import (
            SaaSAzureDevOpsService,
        )

        service = cast(SaaSAzureDevOpsService, azure_devops_service)

        is_user_admin, status = await service.check_user_has_admin_access_to_resource(
            organization=webhook.organization,
            project=webhook.project,
        )

        logger.info(
            'Is user admin',
            extra={
                'is_user_admin': is_user_admin,
                'status': status,
                'organization': webhook.organization,
                'project': webhook.project,
            },
        )

        self.determine_if_rate_limited(status)

        if not is_user_admin:
            await webhook_store.delete_webhook(webhook)
            raise BreakLoopException()

    async def check_if_webhook_already_exists_on_resource(
        self,
        azure_devops_service: GitService,
        webhook: AzureDevOpsWebhook,
        webhook_store: AzureDevOpsWebhookStore,
    ) -> None:
        """Check whether a webhook already exists on the resource."""
        from integrations.azure_devops.azure_devops_service import (
            SaaSAzureDevOpsService,
        )

        service = cast(SaaSAzureDevOpsService, azure_devops_service)

        does_webhook_exist, status = await service.check_webhook_exists_on_resource(
            organization=webhook.organization,
            project=webhook.project,
            webhook_url=AZURE_DEVOPS_WEBHOOK_URL,
        )

        logger.info(
            'Does webhook already exist',
            extra={
                'does_webhook_exist': does_webhook_exist,
                'status': status,
                'organization': webhook.organization,
                'project': webhook.project,
            },
        )

        self.determine_if_rate_limited(status)

        if does_webhook_exist != webhook.webhook_exists:
            await webhook_store.update_webhook(
                webhook, {'webhook_exists': does_webhook_exist}
            )

        if does_webhook_exist:
            raise BreakLoopException()

    async def verify_conditions_are_met(
        self,
        azure_devops_service: GitService,
        webhook: AzureDevOpsWebhook,
        webhook_store: AzureDevOpsWebhookStore,
    ) -> None:
        """Verify all conditions for installing a webhook are met."""
        await self.check_if_resource_exists(
            azure_devops_service=azure_devops_service,
            webhook=webhook,
            webhook_store=webhook_store,
        )

        await self.check_if_user_has_admin_access_to_resource(
            azure_devops_service=azure_devops_service,
            webhook=webhook,
            webhook_store=webhook_store,
        )

        await self.check_if_webhook_already_exists_on_resource(
            azure_devops_service=azure_devops_service,
            webhook=webhook,
            webhook_store=webhook_store,
        )

    async def create_new_webhook(
        self,
        azure_devops_service: GitService,
        webhook: AzureDevOpsWebhook,
        webhook_store: AzureDevOpsWebhookStore,
    ) -> None:
        """Install webhook on the Azure DevOps repository."""
        from integrations.azure_devops.azure_devops_service import (
            SaaSAzureDevOpsService,
        )

        service = cast(SaaSAzureDevOpsService, azure_devops_service)

        # Generate unique secret and UUID for this webhook
        webhook_secret = f'{webhook.user_id}-{str(uuid4())}'
        webhook_uuid = str(uuid4())

        subscription_id, status = await service.install_webhook(
            organization=webhook.organization,
            project=webhook.project,
            repository_id=webhook.repository_id,
            webhook_url=AZURE_DEVOPS_WEBHOOK_URL,
            webhook_secret=webhook_secret,
            webhook_uuid=webhook_uuid,
            event_types=EVENT_TYPES,
        )

        logger.info(
            'Creating new webhook',
            extra={
                'subscription_id': subscription_id,
                'status': status,
                'organization': webhook.organization,
                'project': webhook.project,
                'repository_id': webhook.repository_id,
            },
        )

        self.determine_if_rate_limited(status)

        if subscription_id:
            await webhook_store.update_webhook(
                webhook=webhook,
                update_fields={
                    'webhook_secret': webhook_secret,
                    'webhook_exists': True,
                    'webhook_url': AZURE_DEVOPS_WEBHOOK_URL,
                    'webhook_uuid': webhook_uuid,
                    'subscription_id': subscription_id,
                    'status': WebhookStatus.VERIFIED,
                },
            )

            logger.info(
                f'Installed webhook for {webhook.user_id} on '
                f'{webhook.organization}/{webhook.project}/{webhook.repository_id}'
            )

    async def install_webhooks(self) -> None:
        """Main entry point for installing Azure DevOps webhooks.

        Periodically checks conditions for installing webhooks on repositories.
        Rows with valid conditions will have (webhook_exists=False, status=PENDING).

        Conditions checked:
            1. Resource exists - user could have deleted repository
            2. User has admin access - user's permissions could have changed
            3. Webhook doesn't already exist - avoid duplicates
        """
        from integrations.azure_devops.azure_devops_service import (
            SaaSAzureDevOpsService,
        )

        # Get an instance of the webhook store
        webhook_store = await AzureDevOpsWebhookStore.get_instance()

        # Load chunks of rows that need processing (webhook_exists == False)
        webhooks_to_process = await self.fetch_rows(webhook_store)

        logger.info(
            'Processing Azure DevOps webhook chunks',
            extra={'webhooks_to_process_count': len(webhooks_to_process)},
        )

        for webhook in webhooks_to_process:
            try:
                user_id = webhook.user_id

                # Create service instance for this user
                azure_devops_service_impl = AzureDevOpsServiceImpl(
                    external_auth_id=user_id
                )

                if not isinstance(azure_devops_service_impl, SaaSAzureDevOpsService):
                    logger.warning(
                        'Only SaaSAzureDevOpsService is supported for webhook installation'
                    )
                    continue

                azure_devops_service = cast(
                    SaaSAzureDevOpsService, azure_devops_service_impl
                )

                # Set the organization from the webhook
                azure_devops_service.organization = webhook.organization

                await self.verify_conditions_are_met(
                    azure_devops_service=azure_devops_service,
                    webhook=webhook,
                    webhook_store=webhook_store,
                )

                # Conditions have been met for installing webhook
                await self.create_new_webhook(
                    azure_devops_service=azure_devops_service,
                    webhook=webhook,
                    webhook_store=webhook_store,
                )

            except BreakLoopException:
                pass  # Continue processing but still update last_synced
            except Exception as e:
                logger.warning(
                    f'Error processing webhook: {e}',
                    extra={
                        'webhook_id': getattr(webhook, 'id', None),
                        'organization': webhook.organization,
                        'project': webhook.project,
                        'repository_id': webhook.repository_id,
                    },
                    exc_info=True,
                )
            finally:
                # Always update last_synced after processing (success or failure)
                # to prevent immediate reprocessing of the same webhook
                try:
                    await webhook_store.update_last_synced(webhook)
                except Exception as e:
                    logger.warning(
                        'Failed to update last_synced for webhook',
                        extra={
                            'webhook_id': getattr(webhook, 'id', None),
                            'organization': webhook.organization,
                            'project': webhook.project,
                            'error': str(e),
                        },
                    )


if __name__ == '__main__':
    status_verifier = VerifyWebhookStatus()
    asyncio.run(status_verifier.install_webhooks())
