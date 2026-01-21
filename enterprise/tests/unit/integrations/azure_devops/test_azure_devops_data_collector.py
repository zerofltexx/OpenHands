"""Tests for `AzureDevOpsDataCollector.process_payload`.

Only the PR-completion tracking path is wired today; non-completed events and
the disabled-by-env-var path must be no-ops.
"""

from unittest.mock import MagicMock, patch

import pytest
from integrations.azure_devops.data_collector import AzureDevOpsDataCollector
from integrations.models import Message, SourceType
from integrations.types import PRStatus

_COMPLETED_PR_PAYLOAD = {
    'eventType': 'git.pullrequest.updated',
    'resource': {
        'pullRequestId': 7,
        'status': 'completed',
        'mergeStatus': 'succeeded',
        'creationDate': '2026-01-21T10:00:00.0000000Z',
        'closedDate': '2026-01-21T11:00:00.0000000Z',
        'repository': {
            'id': 'repo-uuid',
            'name': 'myrepo',
            'project': {'name': 'Widgets'},
        },
    },
}

_ABANDONED_PR_PAYLOAD = {
    **_COMPLETED_PR_PAYLOAD,
    'resource': {
        **_COMPLETED_PR_PAYLOAD['resource'],
        'status': 'abandoned',
        'mergeStatus': 'notSet',
    },
}

_ACTIVE_PR_PAYLOAD = {
    **_COMPLETED_PR_PAYLOAD,
    'resource': {
        **_COMPLETED_PR_PAYLOAD['resource'],
        'status': 'active',
    },
}


def _msg(payload: dict) -> Message:
    return Message(source=SourceType.AZURE_DEVOPS, message=payload)


@pytest.fixture
def patched_store():
    fake_store = MagicMock()
    fake_store.insert_pr = MagicMock()
    with patch(
        'integrations.azure_devops.data_collector.OpenhandsPRStore.get_instance',
        return_value=fake_store,
    ):
        yield fake_store


def _enable_collection(monkeypatch):
    monkeypatch.setattr(
        'integrations.azure_devops.data_collector.COLLECT_AZURE_DEVOPS_INTERACTIONS',
        True,
    )


def test_process_payload_skipped_when_collection_disabled(monkeypatch, patched_store):
    monkeypatch.setattr(
        'integrations.azure_devops.data_collector.COLLECT_AZURE_DEVOPS_INTERACTIONS',
        False,
    )
    AzureDevOpsDataCollector().process_payload(_msg(_COMPLETED_PR_PAYLOAD))
    patched_store.insert_pr.assert_not_called()


def test_process_payload_inserts_merged_pr_on_completed_succeeded(
    monkeypatch, patched_store
):
    _enable_collection(monkeypatch)

    AzureDevOpsDataCollector().process_payload(_msg(_COMPLETED_PR_PAYLOAD))

    patched_store.insert_pr.assert_called_once()
    pr = patched_store.insert_pr.call_args.args[0]
    assert pr.pr_number == 7
    assert pr.repo_name == 'Widgets/myrepo'
    assert pr.repo_id == 'repo-uuid'
    assert pr.status == PRStatus.MERGED
    assert pr.merged is True
    assert pr.provider == 'azure_devops'


def test_process_payload_records_abandoned_as_closed_not_merged(
    monkeypatch, patched_store
):
    _enable_collection(monkeypatch)

    AzureDevOpsDataCollector().process_payload(_msg(_ABANDONED_PR_PAYLOAD))

    pr = patched_store.insert_pr.call_args.args[0]
    assert pr.status == PRStatus.CLOSED
    assert pr.merged is False


def test_process_payload_no_op_for_active_pr(monkeypatch, patched_store):
    """Active PR updates (status=active) are noisy; we only record terminal states."""
    _enable_collection(monkeypatch)

    AzureDevOpsDataCollector().process_payload(_msg(_ACTIVE_PR_PAYLOAD))

    patched_store.insert_pr.assert_not_called()


def test_process_payload_no_op_for_unrelated_event(monkeypatch, patched_store):
    _enable_collection(monkeypatch)

    AzureDevOpsDataCollector().process_payload(
        _msg({'eventType': 'workitem.updated', 'resource': {}})
    )

    patched_store.insert_pr.assert_not_called()


def test_process_payload_swallows_exceptions(monkeypatch, patched_store):
    """A bad payload must not bubble up — we log + move on."""
    _enable_collection(monkeypatch)
    patched_store.insert_pr.side_effect = RuntimeError('db down')

    # Should not raise.
    AzureDevOpsDataCollector().process_payload(_msg(_COMPLETED_PR_PAYLOAD))
