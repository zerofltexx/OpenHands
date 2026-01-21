"""Tests for the pure helpers in `integrations.azure_devops.utils`."""

import pytest
from integrations.azure_devops.utils import (
    extract_organization_from_payload,
    extract_organization_from_url,
    extract_project_from_url,
    extract_work_item_id_from_url,
    parse_azure_devops_url,
    strip_html_tags,
)


def test_strip_html_tags_returns_empty_for_falsy():
    assert strip_html_tags('') == ''
    assert strip_html_tags(None) == ''  # type: ignore[arg-type]


def test_strip_html_tags_decodes_entities_and_collapses_whitespace():
    html = '<div>Hello&nbsp;<b>world</b>&amp; friends</div>'
    assert strip_html_tags(html) == 'Hello world & friends'


def test_strip_html_tags_returns_original_on_parse_failure(monkeypatch):
    """If BeautifulSoup blows up we return the original string, not None."""

    def boom(*_args, **_kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr('bs4.BeautifulSoup', boom)
    assert strip_html_tags('<x>fallback</x>') == '<x>fallback</x>'


@pytest.mark.parametrize(
    'url,expected',
    [
        ('https://dev.azure.com/MyOrg/MyProject/_apis/foo', ('MyOrg', 'MyProject')),
        ('https://dev.azure.com/MyOrg/', ('MyOrg', '')),
        ('https://dev.azure.com/', ('', '')),
        ('https://example.com/foo/bar', ('', '')),
        ('', ('', '')),
    ],
)
def test_parse_azure_devops_url(url, expected):
    assert parse_azure_devops_url(url) == expected


def test_url_extractors_delegate_to_parse():
    url = 'https://dev.azure.com/Acme/Widgets/_apis/wit/workItems/42'
    assert extract_organization_from_url(url) == 'Acme'
    assert extract_project_from_url(url) == 'Widgets'


@pytest.mark.parametrize(
    'url,expected',
    [
        # the digits BEFORE /updates/ are the work item; the trailing 49 must NOT win
        (
            'https://dev.azure.com/Acme/Widgets/_apis/wit/workItems/1254/updates/49',
            1254,
        ),
        ('https://dev.azure.com/Acme/Widgets/_apis/wit/workItems/7', 7),
        ('https://dev.azure.com/Acme/Widgets/_apis/wit/workItems/abc', None),
        ('https://dev.azure.com/Acme/Widgets/_apis/git/pullrequests/9', None),
        ('', None),
    ],
)
def test_extract_work_item_id_from_url(url, expected):
    assert extract_work_item_id_from_url(url) == expected


def test_extract_organization_from_payload_uses_resourceContainers():
    payload = {
        'resourceContainers': {
            'collection': {'baseUrl': 'https://dev.azure.com/Aingineer/'}
        }
    }
    assert extract_organization_from_payload(payload) == 'Aingineer'


def test_extract_organization_from_payload_returns_empty_when_missing():
    assert extract_organization_from_payload({}) == ''
    assert extract_organization_from_payload({'resourceContainers': {}}) == ''
