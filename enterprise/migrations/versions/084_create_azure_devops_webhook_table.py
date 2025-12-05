"""create azure devops webhook table

Revision ID: 084
Revises: 083
Create Date: 2025-12-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '084'
down_revision: Union[str, None] = '083'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'azure_devops_webhook',
        sa.Column(
            'id', sa.Integer(), nullable=False, primary_key=True, autoincrement=True
        ),
        # Azure DevOps resource identifiers
        sa.Column('organization', sa.String(), nullable=False),
        sa.Column('project', sa.String(), nullable=False),
        sa.Column('repository_id', sa.String(), nullable=False),
        # User who installed the webhook
        sa.Column('user_id', sa.String(), nullable=False),
        # Webhook state
        sa.Column('webhook_exists', sa.Boolean(), nullable=False, default=False),
        # Azure DevOps subscription ID (returned when creating service hook)
        sa.Column('subscription_id', sa.String(), nullable=True),
        # Webhook configuration
        sa.Column('webhook_url', sa.String(), nullable=True),
        sa.Column('webhook_secret', sa.String(), nullable=True),
        sa.Column('webhook_uuid', sa.String(), nullable=True),
        # Status tracking (WebhookStatus enum: 0=PENDING, 1=VERIFIED, 2=RATE_LIMITED, 3=INVALID)
        sa.Column('status', sa.Integer(), nullable=False, default=0),
        # Timestamp for rate limiting and processing order
        sa.Column(
            'last_synced',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=True,
        ),
    )

    # Create indexes for faster lookups
    op.create_index(
        'ix_azure_devops_webhook_user_id', 'azure_devops_webhook', ['user_id']
    )
    op.create_index(
        'ix_azure_devops_webhook_organization', 'azure_devops_webhook', ['organization']
    )
    op.create_index(
        'ix_azure_devops_webhook_webhook_uuid', 'azure_devops_webhook', ['webhook_uuid']
    )
    op.create_index(
        'ix_azure_devops_webhook_subscription_id',
        'azure_devops_webhook',
        ['subscription_id'],
    )

    # Composite unique constraint to ensure one webhook per repository
    op.create_unique_constraint(
        'uq_azure_devops_webhook_repo',
        'azure_devops_webhook',
        ['organization', 'project', 'repository_id'],
    )


def downgrade() -> None:
    # Drop the constraints and indexes first before dropping the table
    op.drop_constraint(
        'uq_azure_devops_webhook_repo', 'azure_devops_webhook', type_='unique'
    )
    op.drop_index(
        'ix_azure_devops_webhook_subscription_id', table_name='azure_devops_webhook'
    )
    op.drop_index(
        'ix_azure_devops_webhook_webhook_uuid', table_name='azure_devops_webhook'
    )
    op.drop_index(
        'ix_azure_devops_webhook_organization', table_name='azure_devops_webhook'
    )
    op.drop_index('ix_azure_devops_webhook_user_id', table_name='azure_devops_webhook')
    op.drop_table('azure_devops_webhook')
