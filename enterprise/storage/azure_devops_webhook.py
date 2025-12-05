from enum import IntEnum

from sqlalchemy import Boolean, Column, DateTime, Integer, String, text
from storage.base import Base


class WebhookStatus(IntEnum):
    PENDING = 0  # Conditions for installation webhook need checking
    VERIFIED = 1  # Conditions are met for installing webhook
    RATE_LIMITED = 2  # API was rate limited, failed to check
    INVALID = 3  # Unexpected error occur when checking (keycloak connection, etc)


class AzureDevOpsWebhook(Base):  # type: ignore
    """
    Represents an Azure DevOps webhook (service hook subscription) configuration for a repository.

    Azure DevOps uses Service Hook Subscriptions to send webhook events.
    Each subscription is scoped to an organization/project/repository.
    """

    __tablename__ = 'azure_devops_webhook'
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Azure DevOps resource identifiers
    # Format: {organization}/{project}/{repository}
    organization = Column(String, nullable=False)
    project = Column(String, nullable=False)
    repository_id = Column(String, nullable=False)  # Can be repo name or GUID

    # Composite unique key to ensure one webhook per repo
    # The unique constraint is defined in the migration

    # User who installed the webhook (first user to add this repo)
    user_id = Column(String, nullable=False)

    # Webhook state tracking
    webhook_exists = Column(Boolean, nullable=False, default=False)

    # Azure DevOps subscription ID (returned when creating service hook)
    subscription_id = Column(String, nullable=True)

    # Webhook configuration
    webhook_url = Column(String, nullable=True)
    webhook_secret = Column(String, nullable=True)  # For HMAC verification
    webhook_uuid = Column(String, nullable=True)  # For identifying this webhook installation

    # Status tracking
    status = Column(Integer, nullable=False, default=WebhookStatus.PENDING)

    last_synced = Column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        onupdate=text('CURRENT_TIMESTAMP'),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f'<AzureDevOpsWebhook(id={self.id}, '
            f'org={self.organization}, project={self.project}, '
            f'repo={self.repository_id}, exists={self.webhook_exists})>'
        )

    @property
    def full_repository_path(self) -> str:
        """Returns the full repository path in format: organization/project/repository"""
        return f'{self.organization}/{self.project}/{self.repository_id}'
