"""Azure DevOps ID Resolver — maps emails ↔ Azure DevOps User IDs (VSIDs).

We use the Identities API (NOT Graph API) because:
- Identities API returns 'id' = VSID/Storage Key, which matches webhook payloads.
- Graph API returns 'originId' = Azure AD Object ID, which does NOT match webhooks.

Callers instantiate the resolver per-request and discard, so we don't cache.
"""

from typing import Optional

import httpx
from pydantic import SecretStr

from openhands.app_server.utils.logger import openhands_logger as logger

_IDENTITIES_API_VERSION = '7.1'
_REQUEST_TIMEOUT = 30.0


class AzureDevOpsIdResolver:
    """Resolves user emails ↔ Azure DevOps User IDs (VSIDs) via the Identities API."""

    def __init__(self, token: SecretStr):
        """Args:
        token: Azure DevOps service principal or PAT token with vso.graph scope.
        """
        self._token = token

    async def get_azure_devops_id_from_email(
        self, email: str, organization: str
    ) -> Optional[str]:
        """Look up an Azure DevOps User ID (VSID) by email."""
        if not email or not organization:
            return None

        identities = await self._query_identities(
            organization,
            {'searchFilter': 'General', 'filterValue': email},
        )
        target = email.lower()
        for identity in identities:
            if _extract_email(identity) == target:
                vsid = identity.get('id')
                if vsid:
                    return vsid
        logger.warning(
            f'[AzureDevOpsIdResolver] No matching identity for email {email}'
        )
        return None

    async def get_user_email_from_id(
        self, azure_devops_id: str, organization: str
    ) -> Optional[str]:
        """Look up an email by Azure DevOps User ID (VSID)."""
        if not azure_devops_id or not organization:
            return None

        identities = await self._query_identities(
            organization, {'identityIds': azure_devops_id}
        )
        if not identities:
            logger.warning(
                f'[AzureDevOpsIdResolver] No identity for VSID {azure_devops_id}'
            )
            return None
        return _extract_email(identities[0])

    async def _query_identities(self, organization: str, params: dict) -> list[dict]:
        """Query the Identities API and return the `value` array (or [] on failure)."""
        url = f'https://vssps.dev.azure.com/{organization}/_apis/identities'
        merged_params = {
            'api-version': _IDENTITIES_API_VERSION,
            'queryMembership': 'None',
            **params,
        }
        headers = {'Authorization': f'Bearer {self._token.get_secret_value()}'}
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url, headers=headers, params=merged_params)
                response.raise_for_status()
                return response.json().get('value', [])
        except httpx.HTTPStatusError as e:
            logger.error(
                f'[AzureDevOpsIdResolver] HTTP {e.response.status_code} querying identities: {e.response.text}'
            )
            return []
        except Exception:
            logger.exception('[AzureDevOpsIdResolver] Error querying identities')
            return []


def _extract_email(identity: dict) -> Optional[str]:
    """Extract a lowercase email from an identity record, or None if absent."""
    properties = identity.get('properties') or {}
    account = properties.get('Account')
    if isinstance(account, dict):
        value = (account.get('$value') or '').lower()
        if value:
            return value
    display = (identity.get('providerDisplayName') or '').lower()
    return display if '@' in display else None
