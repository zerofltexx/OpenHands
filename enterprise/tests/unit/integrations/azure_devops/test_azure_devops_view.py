"""Tests for `integrations.azure_devops.azure_devops_view`.

Covers:
- AzureDevOpsBase / AzureDevOpsWorkItem / AzureDevOpsPRComment dataclass construction
  with the exact kwargs the manager passes (catches field-drift regressions).
- initialize_new_conversation contract (returns UUID, sets conversation_id.hex).
- AzureDevOpsFactory trigger detection (assigned work items, PR / inline PR / work
  item comments with @<service-principal> mentions).
"""

from uuid import UUID

import pytest
from integrations.azure_devops.azure_devops_view import (
    AzureDevOpsFactory,
    AzureDevOpsInlinePRComment,
    AzureDevOpsPRComment,
    AzureDevOpsWorkItem,
)
from integrations.models import Message, SourceType
from integrations.types import UserData

SP_GUID = '12345678-1234-1234-1234-123456789ABC'
SP_GUID_BRACKETED = f'@<{SP_GUID}>'


def _user_info() -> UserData:
    return UserData(user_id='alice', username='Alice', keycloak_user_id='kc-alice')


def _empty_payload(event_type: str = '') -> Message:
    return Message(
        source=SourceType.AZURE_DEVOPS,
        message={'eventType': event_type, 'resource': {}},
    )


# ---------------------------------------------------------------------------
# View dataclass construction
# ---------------------------------------------------------------------------


def _common_kwargs() -> dict:
    """Mirror the kwargs the manager passes when constructing a view."""
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
        title='Demo title',
        description='Demo description',
        previous_comments=[],
        installation_id=0,
        issue_number=42,
    )


def test_workitem_construction_with_manager_kwargs():
    """The exact kwargs the manager passes must construct a view without TypeError."""
    view = AzureDevOpsWorkItem(
        **_common_kwargs(),
        work_item_id=42,
        work_item_type='Bug',
        selected_branch='feature/x',
        comment_body='@<...> please look',
        repository_linked=True,
    )
    assert view.work_item_id == 42
    assert view.full_repo_name == 'Acme/Widgets/myrepo'
    # ResolverViewInterface compatibility — issue_number mirrors the work item id
    assert view.issue_number == 42


def test_pr_comment_construction_with_manager_kwargs():
    view = AzureDevOpsPRComment(
        **_common_kwargs(),
        pr_id=7,
        repository_name='myrepo',
        branch_name='feature/x',
        thread_id=99,
    )
    assert view.pr_id == 7
    assert view.thread_id == 99


def test_inline_pr_comment_carries_thread_context():
    view = AzureDevOpsInlinePRComment(
        **_common_kwargs(),
        pr_id=7,
        repository_name='myrepo',
        branch_name='feature/x',
        thread_id=99,
        thread_context={
            'filePath': 'src/x.py',
            'rightFileEnd': {'line': 12},
        },
    )
    assert view.thread_context['filePath'] == 'src/x.py'


@pytest.mark.asyncio
async def test_initialize_new_conversation_returns_uuid_and_sets_hex_field():
    """Post-V1 migration the contract is `UUID` (not `ConversationMetadata`)."""
    view = AzureDevOpsWorkItem(
        **_common_kwargs(),
        work_item_id=42,
        work_item_type='Bug',
        selected_branch='main',
        comment_body=None,
        repository_linked=True,
    )

    convo_id = await view.initialize_new_conversation()

    assert isinstance(convo_id, UUID)
    # The view caches the hex representation for subsequent string contexts.
    assert view.conversation_id == convo_id.hex


# ---------------------------------------------------------------------------
# AzureDevOpsFactory.is_assigned_work_item
# ---------------------------------------------------------------------------


def _workitem_assigned_message(*, event_type: str, assigned_value) -> Message:
    """Build a workitem.{created,updated} payload with a given System.AssignedTo value."""
    return Message(
        source=SourceType.AZURE_DEVOPS,
        message={
            'eventType': event_type,
            'resource': {'fields': {'System.AssignedTo': assigned_value}},
        },
    )


def test_is_assigned_work_item_true_for_created_with_sp_guid():
    msg = _workitem_assigned_message(
        event_type='workitem.created',
        assigned_value=f'OpenHands <{SP_GUID}>',
    )
    assert AzureDevOpsFactory.is_assigned_work_item(msg, SP_GUID)


def test_is_assigned_work_item_true_for_updated_change_object():
    msg = _workitem_assigned_message(
        event_type='workitem.updated',
        assigned_value={
            'oldValue': f'Bob <{UUID(int=0)}>',
            'newValue': f'OpenHands <{SP_GUID}>',
        },
    )
    assert AzureDevOpsFactory.is_assigned_work_item(msg, SP_GUID)


def test_is_assigned_work_item_false_for_updated_without_new_value():
    msg = _workitem_assigned_message(
        event_type='workitem.updated',
        assigned_value={'oldValue': f'Bob <{SP_GUID}>'},
    )
    assert not AzureDevOpsFactory.is_assigned_work_item(msg, SP_GUID)


def test_is_assigned_work_item_false_for_unrelated_event():
    msg = _workitem_assigned_message(
        event_type='workitem.deleted',
        assigned_value=f'OpenHands <{SP_GUID}>',
    )
    assert not AzureDevOpsFactory.is_assigned_work_item(msg, SP_GUID)


def test_is_assigned_work_item_false_when_no_sp_id_provided():
    """No SP ID → cannot verify, returns False (refuses to act)."""
    msg = _workitem_assigned_message(
        event_type='workitem.created',
        assigned_value=f'OpenHands <{SP_GUID}>',
    )
    assert not AzureDevOpsFactory.is_assigned_work_item(msg, None)


def test_is_assigned_work_item_supports_member_subject_id_pair():
    """SP IDs may arrive as 'memberId,subjectId' — match against either."""
    member_id = UUID(int=1).hex.upper()
    msg = _workitem_assigned_message(
        event_type='workitem.created',
        assigned_value=f'OpenHands <{member_id}>',
    )
    assert AzureDevOpsFactory.is_assigned_work_item(msg, f'{member_id},{SP_GUID}')


# ---------------------------------------------------------------------------
# AzureDevOpsFactory.is_pr_comment / is_inline_pr_comment
# ---------------------------------------------------------------------------


def _pr_comment_message(*, content: str, inline: bool = False) -> Message:
    comment = {'content': content}
    if inline:
        comment['threadContext'] = {
            'filePath': 'src/x.py',
            'rightFileEnd': {'line': 5},
        }
    return Message(
        source=SourceType.AZURE_DEVOPS,
        message={
            'eventType': 'ms.vss-code.git-pullrequest-comment-event',
            'resource': {'comment': comment},
        },
    )


def test_is_pr_comment_detects_mention_with_sp_id():
    msg = _pr_comment_message(content=f'Hey {SP_GUID_BRACKETED} please help')
    assert AzureDevOpsFactory.is_pr_comment(msg, service_principal_id=SP_GUID)


def test_is_pr_comment_ignores_inline_threads_when_inline_false():
    msg = _pr_comment_message(content=f'{SP_GUID_BRACKETED} please help', inline=True)
    assert not AzureDevOpsFactory.is_pr_comment(
        msg, inline=False, service_principal_id=SP_GUID
    )


def test_is_inline_pr_comment_picks_up_inline_threads():
    msg = _pr_comment_message(content=f'{SP_GUID_BRACKETED} please help', inline=True)
    assert AzureDevOpsFactory.is_inline_pr_comment(msg, service_principal_id=SP_GUID)


def test_is_pr_comment_no_mention_no_match():
    msg = _pr_comment_message(content='Just a regular comment')
    assert not AzureDevOpsFactory.is_pr_comment(msg, service_principal_id=SP_GUID)


def test_is_pr_comment_falls_back_to_any_guid_when_no_sp_id():
    """Without a known SP id, ANY @<guid> mention is treated as a request."""
    msg = _pr_comment_message(content=f'Hey {SP_GUID_BRACKETED} please help')
    assert AzureDevOpsFactory.is_pr_comment(msg, service_principal_id=None)


# ---------------------------------------------------------------------------
# AzureDevOpsFactory.is_work_item_comment
# ---------------------------------------------------------------------------


def _workitem_comment_message(*, history) -> Message:
    return Message(
        source=SourceType.AZURE_DEVOPS,
        message={
            'eventType': 'workitem.updated',
            'resource': {'fields': {'System.History': history}},
        },
    )


def test_is_work_item_comment_detects_mention_in_history_newvalue():
    msg = _workitem_comment_message(
        history={'newValue': f'{SP_GUID_BRACKETED} fix this'}
    )
    assert AzureDevOpsFactory.is_work_item_comment(msg, service_principal_id=SP_GUID)


def test_is_work_item_comment_false_without_sp_id():
    """SP id is required — without it we can't verify and return False."""
    msg = _workitem_comment_message(
        history={'newValue': f'{SP_GUID_BRACKETED} fix this'}
    )
    assert not AzureDevOpsFactory.is_work_item_comment(msg, service_principal_id=None)


def test_is_work_item_comment_false_when_no_history_field():
    msg = _workitem_comment_message(history=None)
    assert not AzureDevOpsFactory.is_work_item_comment(
        msg, service_principal_id=SP_GUID
    )
