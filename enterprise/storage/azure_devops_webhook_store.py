from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, asc, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker
from storage.azure_devops_webhook import AzureDevOpsWebhook
from storage.database import a_session_maker

from openhands.core.logger import openhands_logger as logger


@dataclass
class AzureDevOpsWebhookStore:
    a_session_maker: sessionmaker = a_session_maker

    async def store_webhooks(self, webhook_details: list[AzureDevOpsWebhook]) -> None:
        """Store list of webhook details in db using UPSERT pattern.

        Uses composite unique constraint on (organization, project, repository_id)
        to ensure only one webhook per repository.

        Args:
            webhook_details: List of AzureDevOpsWebhook objects to store

        Notes:
            1. Uses UPSERT (INSERT ... ON CONFLICT) to efficiently handle duplicates
            2. Leverages database-level constraints for uniqueness
            3. Performs the operation in a single database transaction
        """
        if not webhook_details:
            return

        async with self.a_session_maker() as session:
            async with session.begin():
                # Convert AzureDevOpsWebhook objects to dictionaries for the insert
                values = [
                    {
                        k: v
                        for k, v in webhook.__dict__.items()
                        if not k.startswith('_') and k != 'id'
                    }
                    for webhook in webhook_details
                ]

                if values:
                    stmt = insert(AzureDevOpsWebhook).values(values)
                    # Use composite unique constraint for conflict detection
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=['organization', 'project', 'repository_id']
                    )
                    await session.execute(stmt)

    async def update_webhook(
        self, webhook: AzureDevOpsWebhook, update_fields: dict
    ) -> None:
        """Update a webhook entry based on organization/project/repository_id.

        Args:
            webhook: AzureDevOpsWebhook object with identifiers
            update_fields: Dictionary of fields to update
        """
        async with self.a_session_maker() as session:
            async with session.begin():
                stmt = (
                    update(AzureDevOpsWebhook)
                    .where(
                        and_(
                            AzureDevOpsWebhook.organization == webhook.organization,
                            AzureDevOpsWebhook.project == webhook.project,
                            AzureDevOpsWebhook.repository_id == webhook.repository_id,
                        )
                    )
                    .values(**update_fields)
                )
                await session.execute(stmt)

    async def delete_webhook(self, webhook: AzureDevOpsWebhook) -> None:
        """Delete a webhook entry based on organization/project/repository_id.

        Args:
            webhook: AzureDevOpsWebhook object with identifiers
        """
        logger.info(
            'Attempting to delete Azure DevOps webhook',
            extra={
                'organization': webhook.organization,
                'project': webhook.project,
                'repository_id': webhook.repository_id,
                'user_id': getattr(webhook, 'user_id', None),
            },
        )

        async with self.a_session_maker() as session:
            async with session.begin():
                query = AzureDevOpsWebhook.__table__.delete().where(
                    and_(
                        AzureDevOpsWebhook.organization == webhook.organization,
                        AzureDevOpsWebhook.project == webhook.project,
                        AzureDevOpsWebhook.repository_id == webhook.repository_id,
                    )
                )

                result = await session.execute(query)
                rows_deleted = result.rowcount

                if rows_deleted > 0:
                    logger.info(
                        'Successfully deleted Azure DevOps webhook',
                        extra={
                            'organization': webhook.organization,
                            'project': webhook.project,
                            'repository_id': webhook.repository_id,
                            'rows_deleted': rows_deleted,
                        },
                    )
                else:
                    logger.warning(
                        'No Azure DevOps webhook found to delete',
                        extra={
                            'organization': webhook.organization,
                            'project': webhook.project,
                            'repository_id': webhook.repository_id,
                        },
                    )

    async def update_last_synced(self, webhook: AzureDevOpsWebhook) -> None:
        """Update the last_synced timestamp for a webhook to current time.

        This should be called after processing a webhook to ensure it's not
        immediately reprocessed in the next batch.

        Args:
            webhook: AzureDevOpsWebhook object with identifiers
        """
        await self.update_webhook(webhook, {'last_synced': text('CURRENT_TIMESTAMP')})

    async def filter_rows(
        self,
        limit: int = 100,
    ) -> list[AzureDevOpsWebhook]:
        """Retrieve rows that need processing (webhook doesn't exist on resource).

        Args:
            limit: Maximum number of rows to retrieve (default: 100)

        Returns:
            List of AzureDevOpsWebhook objects that need processing
        """
        async with self.a_session_maker() as session:
            query = (
                select(AzureDevOpsWebhook)
                .where(AzureDevOpsWebhook.webhook_exists.is_(False))
                .order_by(asc(AzureDevOpsWebhook.last_synced))
                .limit(limit)
            )
            result = await session.execute(query)
            webhooks = result.scalars().all()

            return list(webhooks)

    async def get_webhook_secret(
        self, webhook_uuid: str, user_id: str
    ) -> str | None:
        """Get webhook secret given the webhook uuid and user id.

        Args:
            webhook_uuid: The unique webhook installation identifier
            user_id: The user who installed the webhook

        Returns:
            The webhook secret if found, None otherwise
        """
        async with self.a_session_maker() as session:
            query = (
                select(AzureDevOpsWebhook)
                .where(
                    and_(
                        AzureDevOpsWebhook.user_id == user_id,
                        AzureDevOpsWebhook.webhook_uuid == webhook_uuid,
                    )
                )
                .limit(1)
            )

            result = await session.execute(query)
            webhooks: list[AzureDevOpsWebhook] = list(result.scalars().all())

            if len(webhooks):
                return webhooks[0].webhook_secret
            return None

    async def get_webhook_by_subscription_id(
        self, subscription_id: str
    ) -> AzureDevOpsWebhook | None:
        """Get webhook by Azure DevOps subscription ID.

        Args:
            subscription_id: The Azure DevOps service hook subscription ID

        Returns:
            The webhook if found, None otherwise
        """
        async with self.a_session_maker() as session:
            query = (
                select(AzureDevOpsWebhook)
                .where(AzureDevOpsWebhook.subscription_id == subscription_id)
                .limit(1)
            )

            result = await session.execute(query)
            webhook = result.scalar_one_or_none()
            return webhook

    @classmethod
    async def get_instance(cls) -> AzureDevOpsWebhookStore:
        """Get an instance of the AzureDevOpsWebhookStore.

        Returns:
            An instance of AzureDevOpsWebhookStore
        """
        return AzureDevOpsWebhookStore(a_session_maker)
