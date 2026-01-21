import hashlib
import json
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from integrations.azure_devops.azure_devops_manager import AzureDevOpsManager
from integrations.azure_devops.data_collector import AzureDevOpsDataCollector
from integrations.models import Message, SourceType
from server.auth.token_manager import TokenManager
from storage.redis import get_redis_client_async

from openhands.app_server.utils.logger import openhands_logger as logger

# Environment variable to disable Azure DevOps webhooks
AZURE_DEVOPS_WEBHOOKS_ENABLED = os.environ.get(
    'AZURE_DEVOPS_WEBHOOKS_ENABLED', '1'
) in ('1', 'true')

azure_devops_integration_router = APIRouter(prefix='/integration')

# Initialize Azure DevOps manager with TokenManager (matching GitHub/GitLab pattern)
token_manager = TokenManager()
data_collector = AzureDevOpsDataCollector()
azure_devops_manager = AzureDevOpsManager(token_manager, data_collector)


async def verify_api_key(authorization: str | None) -> str:
    """Verify API key from Authorization header and return user_id.

    Users must generate an API key from OpenHands Settings -> API Keys, then
    configure their Azure DevOps Service Hook with HTTP Header
    Name: "Authorization", Value: "Bearer <api_key>".

    Raises:
        HTTPException: 401 Unauthorized if the header is missing/malformed or
            the key is invalid/expired.
    """
    if not authorization or not authorization.startswith('Bearer '):
        logger.warning(
            '[Azure DevOps Webhook] Missing or malformed Authorization header'
        )
        raise HTTPException(
            status_code=401,
            detail='Unauthorized: expected Authorization header "Bearer <api_key>"',
        )

    api_key = authorization[len('Bearer ') :]

    from storage.api_key_store import ApiKeyStore

    api_key_store = ApiKeyStore.get_instance()
    result = await api_key_store.validate_api_key(api_key)

    if result is None:
        logger.warning('[Azure DevOps Webhook] Invalid or expired API key')
        raise HTTPException(
            status_code=401,
            detail='Unauthorized: Invalid or expired API key',
        )

    return result.user_id


@azure_devops_integration_router.post('/azure-devops/events')
async def azure_devops_events(
    request: Request,
    authorization: str | None = Header(None),
):
    """Handle Azure DevOps Service Hook webhook events.

    This endpoint receives Service Hook webhooks from Azure DevOps
    for work item and PR comment events. Authentication is done via
    API key in the Authorization header (Bearer token).

    Args:
        request: The incoming HTTP request
        authorization: Authorization header containing Bearer token (API key)
    """
    logger.info('[Azure DevOps Webhook] Received webhook request')
    logger.debug(f'[Azure DevOps Webhook] Request headers: {dict(request.headers)}')
    logger.debug(
        f'[Azure DevOps Webhook] Request method: {request.method}, URL: {request.url}'
    )

    # Check if Azure DevOps webhooks are enabled
    if not AZURE_DEVOPS_WEBHOOKS_ENABLED:
        logger.info(
            '[Azure DevOps Webhook] Webhooks are disabled by AZURE_DEVOPS_WEBHOOKS_ENABLED environment variable'
        )
        return JSONResponse(
            status_code=200,
            content={'message': 'Azure DevOps webhooks are currently disabled.'},
        )

    try:
        logger.debug('[Azure DevOps Webhook] Parsing request body as JSON')
        payload_data = await request.json()
        logger.debug(
            f'[Azure DevOps Webhook] Payload size: {len(str(payload_data))} bytes'
        )

        user_id = await verify_api_key(authorization)
        logger.info(
            f'[Azure DevOps Webhook] API key verified successfully for user {user_id}'
        )

        # Validate basic payload structure
        if 'eventType' not in payload_data:
            logger.error('[Azure DevOps Webhook] Missing eventType in payload')
            logger.debug(
                f'[Azure DevOps Webhook] Payload keys: {list(payload_data.keys())}'
            )
            return JSONResponse(
                status_code=400,
                content={'error': 'Missing eventType in payload.'},
            )

        event_type = payload_data.get('eventType')
        subscription_id = payload_data.get('subscriptionId')
        notification_id = payload_data.get('notificationId')

        logger.info(
            f'[Azure DevOps Webhook] Event type: {event_type}, '
            f'Subscription ID: {subscription_id}, '
            f'Notification ID: {notification_id}'
        )

        # Log additional event details if available
        if 'resource' in payload_data:
            resource = payload_data['resource']
            logger.debug(
                f'[Azure DevOps Webhook] Resource type: {resource.get("resourceType", "unknown")}'
            )
            if 'url' in resource:
                logger.debug(f'[Azure DevOps Webhook] Resource URL: {resource["url"]}')

        # Deduplication using Redis (native Azure DevOps payload fields)
        # Azure DevOps includes subscriptionId and notificationId in all webhook payloads
        if subscription_id and notification_id:
            dedup_key = f'azure_devops_{subscription_id}_{notification_id}'
            logger.debug(f'[Azure DevOps Webhook] Dedup key (from IDs): {dedup_key}')
        else:
            # Fallback: hash the entire payload when IDs are missing
            dedup_json = json.dumps(payload_data, sort_keys=True)
            dedup_hash = hashlib.sha256(dedup_json.encode()).hexdigest()
            dedup_key = f'azure_devops_msg:{dedup_hash}'
            logger.debug(
                f'[Azure DevOps Webhook] Dedup key (from hash): {dedup_key[:50]}...'
            )

        # Check Redis for duplicate
        logger.debug('[Azure DevOps Webhook] Checking Redis for duplicate event')
        redis = get_redis_client_async()
        created = await redis.set(dedup_key, 1, nx=True, ex=60)
        if not created:
            logger.info(
                f'[Azure DevOps Webhook] Duplicate event ignored: {dedup_key}',
                extra={'event_type': event_type, 'subscription_id': subscription_id},
            )
            return JSONResponse(
                status_code=200,
                content={'message': 'Duplicate Azure DevOps event ignored.'},
            )

        logger.info(
            f'[Azure DevOps Webhook] Processing new webhook event: {event_type}',
            extra={'subscription_id': subscription_id},
        )

        # Create message object
        # Pass payload directly - factory methods expect raw Azure DevOps payload
        logger.debug('[Azure DevOps Webhook] Creating Message object')
        message = Message(
            source=SourceType.AZURE_DEVOPS,
            message=payload_data,
        )
        logger.debug(
            f'[Azure DevOps Webhook] Message object created with source: {message.source}'
        )

        # Process the message
        logger.info('[Azure DevOps Webhook] Sending message to azure_devops_manager')
        try:
            await azure_devops_manager.receive_message(message)
            logger.info('[Azure DevOps Webhook] Message processed successfully')
        except Exception as processing_error:
            logger.error(
                f'[Azure DevOps Webhook] Error processing message: {processing_error}',
                exc_info=True,
            )
            return JSONResponse(
                status_code=400,
                content={'error': 'Error processing webhook payload.'},
            )

        logger.info('[Azure DevOps Webhook] Returning success response')
        return JSONResponse(
            status_code=200,
            content={'message': 'Azure DevOps webhook event received.'},
        )

    except HTTPException as e:
        # Re-raise HTTP exceptions (authentication failures)
        logger.error(
            f'[Azure DevOps Webhook] HTTP exception occurred: {e.status_code} - {e.detail}'
        )
        raise e
    except json.JSONDecodeError as e:
        logger.error(f'[Azure DevOps Webhook] JSON decode error: {e}', exc_info=True)
        return JSONResponse(
            status_code=400,
            content={'error': 'Invalid JSON payload.'},
        )
    except Exception as e:
        logger.exception(f'[Azure DevOps Webhook] Unexpected error: {e}')
        return JSONResponse(
            status_code=400,
            content={'error': 'Invalid payload.'},
        )
