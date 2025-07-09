import pytest
from unittest.mock import MagicMock, patch
from handlers import OptionsHandler, SupportTicketSubmissionHandler


@pytest.fixture
def mock_azure_support():
    mock = MagicMock()
    mock.get_subscription_list.return_value = [
        {'id': 'sub1', 'display_name': 'Subscription 1'}
    ]
    mock.slack_get_support_services_filter_by_prefix.return_value = {
        'Group1': [{'id': 'svc1', 'displayName': 'Service 1'}]
    }
    mock.get_resource_types_by_service_id.return_value = ['type1']
    mock.get_sub_resources_by_resource_type_concurrent.return_value = {
        'rg1': [{'id': 'rid1', 'name': 'Resource 1'}]
    }
    mock.get_problem_classifications_list.return_value = [
        MagicMock(id='id1', display_name='Group / Problem')
    ]
    return mock


@pytest.fixture
def options_handler(mock_azure_support):
    return OptionsHandler(MagicMock(), mock_azure_support)


def test_get_select_azure_sub(options_handler):
    options = options_handler.get_select_azure_sub("")
    assert options[0]["value"] == "sub1"


def test_get_select_azure_service(options_handler):
    result = options_handler.get_select_azure_service("", {})
    assert isinstance(result, list)
    assert result[0]["label"]["text"] == "Group1"


def test_get_select_azure_subscription_resources_mapped(options_handler):
    private_metadata = {'select_azure_subscription': 'sub1', 'select_azure_service': 'svc1'}
    result = options_handler.get_select_azure_subscription_resources_mapped(private_metadata)
    assert isinstance(result, list)
    assert result[-1]["label"]["text"].startswith("General question")


def test_get_problem_classifications_options_option_groups(options_handler, mock_azure_support):
    res = options_handler.get_problem_classifications_options('subid', 'svc1')
    assert res["type"] == "option_groups"
    assert "Group" in res["values"]


def test_get_select_azure_service_problem_classifications_options(options_handler, mock_azure_support):
    private_metadata = {'select_azure_subscription': 'subid', 'select_azure_service': 'svc1'}
    res = options_handler.get_select_azure_service_problem_classifications(private_metadata)
    assert "type" in res
    assert "values" in res


def test_support_ticket_submission_handler_handle():
    mock_azure_support = MagicMock()
    mock_azure_support.get_resource_id_by_resource_hash.return_value = "resource_id"
    mock_client = MagicMock()
    mock_executor = MagicMock()
    data = {'select_azure_subscription': 'sub1', 'select_azure_service': 'svc1', 'select_azure_resource': 'hash'}
    private_metadata = {'user_id': 'U1'}
    handler = SupportTicketSubmissionHandler(data, private_metadata, mock_azure_support, mock_client, mock_executor)
    handler.handle()
    mock_executor.submit.assert_called()
