import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from integrations.models import Message, SourceType
from server.auth.token_manager import TokenManager
from storage.azure_devops_webhook_store import AzureDevOpsWebhookStore

from openhands.core.logger import openhands_logger as logger
from openhands.server.shared import sio

# Environment variable to disable Azure DevOps webhooks
AZURE_DEVOPS_WEBHOOKS_ENABLED = os.environ.get(
    'AZURE_DEVOPS_WEBHOOKS_ENABLED', '1'
) in ('1', 'true')

azure_devops_integration_router = APIRouter(prefix='/integration')
webhook_store = AzureDevOpsWebhookStore()
token_manager = TokenManager()

# Import the manager when it's created
# For now, we'll process events directly
# from integrations.azure_devops.azure_devops_manager import AzureDevOpsManager
# azure_devops_manager = AzureDevOpsManager(token_manager)


def verify_azure_devops_signature(
    payload: bytes, header_secret: str, stored_secret: str
) -> bool:
    """Verify the webhook signature using HMAC-SHA256.

    Azure DevOps sends the secret in a custom header that we configured
    during webhook creation.

    Args:
        payload: The raw request body
        header_secret: The secret from the webhook header
        stored_secret: The stored secret for this webhook

    Returns:
        bool: True if signatures match
    """
    # For Azure DevOps, we're using a simple secret comparison
    # since we send the secret directly in the header
    return hmac.compare_digest(header_secret, stored_secret)


async def verify_webhook_request(
    header_webhook_secret: str | None,
    webhook_uuid: str | None,
    user_id: str | None,
) -> None:
    """Verify the webhook request is authentic.

    Args:
        header_webhook_secret: Secret from X-OpenHands-Webhook-Secret header
        webhook_uuid: UUID from X-OpenHands-Webhook-ID header
        user_id: User ID from X-OpenHands-User-ID header

    Raises:
        HTTPException: If verification fails
    """
    if not header_webhook_secret or not webhook_uuid or not user_id:
        raise HTTPException(
            status_code=403, detail='Required webhook headers missing!'
        )

    # Look up the stored secret
    stored_secret = await webhook_store.get_webhook_secret(
        webhook_uuid=webhook_uuid, user_id=user_id
    )

    if not stored_secret:
        raise HTTPException(
            status_code=403, detail='Webhook not found or invalid credentials!'
        )

    if not hmac.compare_digest(header_webhook_secret, stored_secret):
        raise HTTPException(status_code=403, detail="Request signatures didn't match!")


def extract_event_info(payload: dict) -> dict:
    """Extract relevant event information from Azure DevOps payload.

    Azure DevOps webhook payloads have different structures depending on the event type.

    Args:
        payload: The webhook payload

    Returns:
        dict: Extracted event information
    """
    event_type = payload.get('eventType', '')
    resource = payload.get('resource', {})

    event_info = {
        'event_type': event_type,
        'subscription_id': payload.get('subscriptionId'),
        'notification_id': payload.get('notificationId'),
    }

    if event_type == 'git.push':
        # Code push event
        repository = resource.get('repository', {})
        event_info.update(
            {
                'repository_id': repository.get('id'),
                'repository_name': repository.get('name'),
                'project_name': repository.get('project', {}).get('name'),
                'pusher': resource.get('pushedBy', {}).get('displayName'),
                'ref_updates': resource.get('refUpdates', []),
            }
        )
    elif event_type.startswith('git.pullrequest'):
        # Pull request event
        event_info.update(
            {
                'repository_id': resource.get('repository', {}).get('id'),
                'repository_name': resource.get('repository', {}).get('name'),
                'project_name': resource.get('repository', {})
                .get('project', {})
                .get('name'),
                'pr_id': resource.get('pullRequestId'),
                'pr_title': resource.get('title'),
                'pr_status': resource.get('status'),
                'source_branch': resource.get('sourceRefName'),
                'target_branch': resource.get('targetRefName'),
                'created_by': resource.get('createdBy', {}).get('displayName'),
            }
        )

    return event_info


@azure_devops_integration_router.post('/azure-devops/events')
async def azure_devops_events(
    request: Request,
    x_openhands_webhook_secret: str = Header(None),
    x_openhands_webhook_id: str = Header(None),
    x_openhands_user_id: str = Header(None),
):
    """Handle incoming Azure DevOps webhook events.

    This endpoint receives Service Hook notifications from Azure DevOps
    for events like code pushes and pull request changes.
    """
    # Check if Azure DevOps webhooks are enabled
    if not AZURE_DEVOPS_WEBHOOKS_ENABLED:
        logger.info(
            'Azure DevOps webhooks are disabled by AZURE_DEVOPS_WEBHOOKS_ENABLED environment variable'
        )
        return JSONResponse(
            status_code=200,
            content={'message': 'Azure DevOps webhooks are currently disabled.'},
        )

    try:
        # Verify the webhook request
        await verify_webhook_request(
            header_webhook_secret=x_openhands_webhook_secret,
            webhook_uuid=x_openhands_webhook_id,
            user_id=x_openhands_user_id,
        )

        payload_data = await request.json()

        # Extract a deduplication key
        # Azure DevOps provides subscriptionId and notificationId
        notification_id = payload_data.get('notificationId')
        subscription_id = payload_data.get('subscriptionId')

        if notification_id and subscription_id:
            dedup_key = f'azure_devops:{subscription_id}:{notification_id}'
        else:
            # Hash entire payload if no notification ID
            dedup_json = json.dumps(payload_data, sort_keys=True)
            dedup_hash = hashlib.sha256(dedup_json.encode()).hexdigest()
            dedup_key = f'azure_devops_msg:{dedup_hash}'

        # Check for duplicate events using Redis
        redis = sio.manager.redis
        created = await redis.set(dedup_key, 1, nx=True, ex=60)
        if not created:
            logger.info('azure_devops_is_duplicate')
            return JSONResponse(
                status_code=200,
                content={'message': 'Duplicate Azure DevOps event ignored.'},
            )

        # Extract event information for logging
        event_info = extract_event_info(payload_data)
        logger.info(
            'Received Azure DevOps webhook event',
            extra={
                'event_type': event_info.get('event_type'),
                'subscription_id': subscription_id,
                'webhook_uuid': x_openhands_webhook_id,
                'user_id': x_openhands_user_id,
            },
        )

        # Create message for processing
        message = Message(
            source=SourceType.AZURE_DEVOPS,
            message={
                'payload': payload_data,
                'webhook_uuid': x_openhands_webhook_id,
                'user_id': x_openhands_user_id,
                'event_info': event_info,
            },
        )

        # TODO: Process the message through an AzureDevOpsManager
        # await azure_devops_manager.receive_message(message)

        # For now, just log that we received it
        logger.info(
            'Azure DevOps webhook processed successfully',
            extra={
                'event_type': event_info.get('event_type'),
                'repository': event_info.get('repository_name'),
                'project': event_info.get('project_name'),
            },
        )

        return JSONResponse(
            status_code=200,
            content={'message': 'Azure DevOps events endpoint reached successfully.'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f'Error processing Azure DevOps event: {e}')
        return JSONResponse(status_code=400, content={'error': 'Invalid payload.'})
