"""Tests for `AzureDevOpsManager`.

Covers the framework-contract surfaces that were previously broken:
- inherits from `Manager[AzureDevOpsViewType]` and declares `manager_type`.
- `send_message` accepts `str` (not `Message`) and dispatches to the right
  Azure DevOps service method based on the view type.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from integrations.azure_devops.azure_devops_manager import AzureDevOpsManager
from integrations.azure_devops.azure_devops_view import (
    AzureDevOpsInlinePRComment,
    AzureDevOpsPRComment,
    AzureDevOpsWorkItem,
)
from integrations.manager import Manager
from integrations.models import Message, SourceType
from integrations.types import UserData


def _user_info() -> UserData:
    return UserData(user_id='alice', username='Alice', keycloak_user_id='kc-alice')


def _empty_payload() -> Message:
    return Message(source=SourceType.AZURE_DEVOPS, message={})


def _make_manager() -> AzureDevOpsManager:
    return AzureDevOpsManager(MagicMock(), MagicMock())


def _common_view_kwargs() -> dict:
    return dict(
        organization='Acme',
        project_name='Widgets',
        full_repo_name='Acme/Widgets/myrepo',
        is_public_repo=False,
        user_info=_user_info(),
        raw_payload=_empty_payload(),
        conversation_id='',
        uuid=None,
        should_extract=True,
        send_summary_instruction=True,
        title='t',
        description='d',
        previous_comments=[],
        installation_id=0,
        issue_number=42,
    )


def _workitem_view() -> AzureDevOpsWorkItem:
    return AzureDevOpsWorkItem(
        **_common_view_kwargs(),
        work_item_id=42,
        work_item_type='Bug',
        selected_branch='main',
        comment_body=None,
        repository_linked=True,
    )


def _pr_view(thread_id: int | None) -> AzureDevOpsPRComment:
    return AzureDevOpsPRComment(
        **_common_view_kwargs(),
        pr_id=7,
        repository_name='myrepo',
        branch_name='feature/x',
        thread_id=thread_id,
    )


def _inline_pr_view() -> AzureDevOpsInlinePRComment:
    return AzureDevOpsInlinePRComment(
        **_common_view_kwargs(),
        pr_id=7,
        repository_name='myrepo',
        branch_name='feature/x',
        thread_id=99,
        thread_context={'filePath': 'src/x.py', 'rightFileEnd': {'line': 5}},
    )


# ---------------------------------------------------------------------------
# Framework contract
# ---------------------------------------------------------------------------


def test_manager_inherits_from_generic_manager_base():
    """The base contract requires Manager[ViewT]; the original PR didn't inherit."""
    assert issubclass(AzureDevOpsManager, Manager)


def test_manager_declares_manager_type_as_azure_devops():
    assert AzureDevOpsManager.manager_type is SourceType.AZURE_DEVOPS


# ---------------------------------------------------------------------------
# send_message dispatch (the signature was previously `Message`, breaking the
# Manager[ViewT] contract; pin the corrected behavior).
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_service():
    """Patch the AzureDevOpsService construction so we can assert on calls."""
    fake_service = MagicMock()
    fake_service.add_work_item_comment = AsyncMock()
    fake_service.add_pr_thread = AsyncMock()
    fake_service.add_pr_comment_to_thread = AsyncMock()
    with patch(
        'integrations.azure_devops.azure_devops_manager.AzureDevOpsService',
        return_value=fake_service,
    ):
        yield fake_service


@pytest.mark.asyncio
async def test_send_message_to_workitem_calls_add_work_item_comment(patched_service):
    manager = _make_manager()
    manager.token_manager.get_azure_devops_service_principal_token = AsyncMock(
        return_value='sp-token'
    )

    await manager.send_message('plain string body', _workitem_view())

    patched_service.add_work_item_comment.assert_awaited_once_with(
        'Acme/Widgets/myrepo', 42, 'plain string body'
    )
    patched_service.add_pr_thread.assert_not_called()
    patched_service.add_pr_comment_to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_to_pr_with_thread_id_replies_in_thread(
    patched_service,
):
    manager = _make_manager()
    manager.token_manager.get_azure_devops_service_principal_token = AsyncMock(
        return_value='sp-token'
    )

    await manager.send_message('reply body', _pr_view(thread_id=99))

    patched_service.add_pr_comment_to_thread.assert_awaited_once_with(
        'Acme/Widgets/myrepo', 7, 99, 'reply body'
    )
    patched_service.add_pr_thread.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_to_pr_without_thread_id_creates_new_thread(
    patched_service,
):
    manager = _make_manager()
    manager.token_manager.get_azure_devops_service_principal_token = AsyncMock(
        return_value='sp-token'
    )

    await manager.send_message('new thread body', _pr_view(thread_id=None))

    patched_service.add_pr_thread.assert_awaited_once_with(
        'Acme/Widgets/myrepo', 7, 'new thread body'
    )
    patched_service.add_pr_comment_to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_to_inline_pr_uses_thread_reply(patched_service):
    """Inline PR comments are AzureDevOpsPRComment subclasses — they should
    reply into the original code-review thread."""
    manager = _make_manager()
    manager.token_manager.get_azure_devops_service_principal_token = AsyncMock(
        return_value='sp-token'
    )

    await manager.send_message('inline reply', _inline_pr_view())

    patched_service.add_pr_comment_to_thread.assert_awaited_once_with(
        'Acme/Widgets/myrepo', 7, 99, 'inline reply'
    )
