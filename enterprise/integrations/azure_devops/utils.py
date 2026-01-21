"""Pure utility helpers for Azure DevOps payload parsing and Keycloak user resolution.

Extracted from `azure_devops_manager.py` to keep the manager focused on
orchestration. None of these need a `Manager` instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr

from openhands.app_server.integrations.provider import ProviderType
from openhands.app_server.utils.logger import openhands_logger as logger

if TYPE_CHECKING:
    from server.auth.token_manager import TokenManager


# -- Payload helpers ----------------------------------------------------------


def strip_html_tags(html_text: str) -> str:
    """Strip HTML tags from text. Azure DevOps stores descriptions in HTML."""
    if not html_text:
        return ''
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html_text, 'html.parser').get_text(
            separator=' ', strip=True
        )
    except Exception as e:
        logger.warning(f'[Azure DevOps] Failed to parse HTML: {e}')
        return html_text


# -- URL parsing --------------------------------------------------------------


def parse_azure_devops_url(url: str) -> tuple[str, str]:
    """Parse an Azure DevOps URL into `(organization, project)`.

    Returns `('', '')` if the URL doesn't match `https://dev.azure.com/<org>/<project>/...`.
    """
    if 'dev.azure.com/' not in url:
        return '', ''
    try:
        parts = url.split('dev.azure.com/', 1)[1].split('/')
        org = parts[0] if len(parts) > 0 else ''
        project = parts[1] if len(parts) > 1 else ''
        return org, project
    except Exception:
        return '', ''


def extract_organization_from_url(url: str) -> str:
    return parse_azure_devops_url(url)[0]


def extract_project_from_url(url: str) -> str:
    return parse_azure_devops_url(url)[1]


def extract_work_item_id_from_url(url: str) -> int | None:
    """Extract the work item ID from an Azure DevOps URL.

    For URLs like `.../_apis/wit/workItems/1254/updates/49` returns `1254`,
    NOT the trailing update id.
    """
    if '/workItems/' not in url:
        return None
    try:
        id_part = url.split('/workItems/', 1)[1].split('/', 1)[0]
        return int(id_part)
    except Exception:
        return None


def extract_organization_from_payload(payload: dict) -> str:
    """Pull the organization name out of a webhook's `resourceContainers.collection.baseUrl`."""
    base_url = (
        payload.get('resourceContainers', {}).get('collection', {}).get('baseUrl', '')
    )
    return extract_organization_from_url(base_url) if base_url else ''


# -- Keycloak resolution ------------------------------------------------------


async def resolve_keycloak_user_id(
    token_manager: 'TokenManager', author_azure_id: str, organization: str
) -> str | None:
    """Resolve an Azure DevOps user GUID to a Keycloak user ID.

    Cascades through three plans:
      A. webhook subscription table lookup
      B. direct Keycloak attribute lookup (`azure_devops_id`)
      C. email lookup via Identities API + opportunistic Keycloak attribute enrichment
    """
    # Plan A — webhook table
    try:
        from sqlalchemy import select
        from storage.azure_devops_webhook import AzureDevOpsWebhook
        from storage.database import a_session_maker

        async with a_session_maker() as session:
            stmt = (
                select(AzureDevOpsWebhook)
                .where(
                    AzureDevOpsWebhook.user_id == str(author_azure_id),
                    AzureDevOpsWebhook.organization == organization,
                )
                .limit(1)
            )
            webhook = (await session.execute(stmt)).scalar_one_or_none()
            if webhook:
                kc_id = await token_manager.get_user_id_from_idp_user_id(
                    str(author_azure_id), ProviderType.AZURE_DEVOPS
                )
                if kc_id:
                    return kc_id
    except Exception as e:
        logger.debug(f'[Azure DevOps] Plan A (webhook lookup) failed: {e}')

    # Plan B — direct Keycloak attribute
    kc_id = await token_manager.get_user_id_from_idp_user_id(
        str(author_azure_id), ProviderType.AZURE_DEVOPS
    )
    if kc_id:
        return kc_id

    # Plan C — email lookup via Identities API
    try:
        sp_token = await token_manager.get_azure_devops_service_principal_token()
        if not sp_token:
            return None

        from integrations.azure_devops.azure_devops_id_resolver import (
            AzureDevOpsIdResolver,
        )

        resolver = AzureDevOpsIdResolver(SecretStr(sp_token))
        author_email = await resolver.get_user_email_from_id(
            str(author_azure_id), organization
        )
        if not author_email:
            return None

        from server.auth.keycloak_manager import get_keycloak_admin

        keycloak_admin = get_keycloak_admin(external=True)
        users = await keycloak_admin.a_get_users({'email': author_email})
        if not users:
            return None

        kc_id = users[0]['id']

        # Opportunistic enrichment: future webhooks resolve via Plan B.
        try:
            await keycloak_admin.a_update_user(
                kc_id,
                {'attributes': {'azure_devops_id': [str(author_azure_id)]}},
            )
        except Exception as e:
            logger.debug(
                f'[Azure DevOps] Failed to enrich Keycloak user with Azure ID: {e}'
            )

        return kc_id
    except Exception as e:
        logger.debug(f'[Azure DevOps] Plan C (email lookup) failed: {e}')
        return None
