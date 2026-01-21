"""Tests for `verify_api_key` and the Azure DevOps webhook endpoint.

The original PR called `validate_api_key` synchronously, returning a coroutine
that was always truthy — so every webhook bypassed authentication in
production. These tests pin the corrected behavior.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from server.routes.integration.azure_devops import verify_api_key
from storage.api_key_store import ApiKeyValidationResult


def _make_result(user_id: str = 'kc-user-42') -> ApiKeyValidationResult:
    return ApiKeyValidationResult(
        user_id=user_id, org_id=None, key_id=1, key_name='test-key'
    )


def _patch_store(*, returns):
    """Patch ApiKeyStore.get_instance().validate_api_key to return `returns`."""
    fake_store = AsyncMock()
    fake_store.validate_api_key = AsyncMock(return_value=returns)
    return patch(
        'storage.api_key_store.ApiKeyStore.get_instance', return_value=fake_store
    )


@pytest.mark.asyncio
async def test_verify_api_key_rejects_missing_header():
    with pytest.raises(HTTPException) as excinfo:
        await verify_api_key(authorization=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_rejects_empty_header():
    with pytest.raises(HTTPException) as excinfo:
        await verify_api_key(authorization='')
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_rejects_non_bearer_scheme():
    with pytest.raises(HTTPException) as excinfo:
        await verify_api_key(authorization='Basic abcdef')
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_rejects_invalid_key():
    """A None result from validate_api_key must produce 401, not pass through."""
    with _patch_store(returns=None):
        with pytest.raises(HTTPException) as excinfo:
            await verify_api_key(authorization='Bearer sk-oh-bogus')
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_returns_user_id_on_valid_key():
    """A valid key must extract `.user_id` from the result and `await` the call.

    Regression test: the original code did `user_id = store.validate_api_key(...)`
    without await, so user_id ended up as a (truthy) coroutine and authentication
    silently passed for every request.
    """
    expected_result = _make_result(user_id='kc-user-42')
    with _patch_store(returns=expected_result):
        user_id = await verify_api_key(authorization='Bearer sk-oh-good-key')
    assert user_id == 'kc-user-42'


@pytest.mark.asyncio
async def test_verify_api_key_actually_awaits_the_async_call():
    """Pin the contract that validate_api_key is awaited (not called sync)."""
    fake_store = AsyncMock()
    fake_store.validate_api_key = AsyncMock(return_value=_make_result())
    with patch(
        'storage.api_key_store.ApiKeyStore.get_instance', return_value=fake_store
    ):
        await verify_api_key(authorization='Bearer sk-oh-x')

    # If we forgot to await, the coroutine would never have been actually called.
    fake_store.validate_api_key.assert_awaited_once_with('sk-oh-x')
