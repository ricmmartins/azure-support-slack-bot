import sys
import os
import pytest
from unittest.mock import patch, MagicMock, mock_open
from azure_support import AzureSupportHelper


@pytest.fixture
def mock_credentials():
    return MagicMock()


@pytest.fixture
def mock_dataset():
    return {
        "Compute": [
            {"id": "service1", "displayName": "VM", "resourceTypes": ["type1", "type2"]}
        ],
        "Storage": [
            {"id": "service2", "displayName": "Blob", "resourceTypes": ["type3"]}
        ]
    }


@patch("builtins.open", new_callable=mock_open, read_data='{"Compute": [{"id": "service1", "displayName": "VM", "resourceTypes": ["type1", "type2"]}], "Storage": [{"id": "service2", "displayName": "Blob", "resourceTypes": ["type3"]}]}')
@patch("azure_support.SubscriptionClient")
@patch("threading.Thread")  # Patch thread to avoid background thread in tests
def test_load_dataset_services_mapped(mock_thread, mock_sub_client, mock_file, mock_credentials):
    helper = AzureSupportHelper(mock_credentials)
    assert "Compute" in helper.dataset
    assert helper.dataset["Compute"][0]["id"] == "service1"


@patch("threading.Thread")
def test_string_to_hash_and_cache(mock_thread, mock_credentials, mock_dataset):
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        value = "some-resource-id"
        h = helper.string_to_hash(value)
        assert isinstance(h, str)
        assert helper.hash_cache[h] == value


@patch("threading.Thread")
def test_get_resource_types_by_service_id(mock_thread, mock_credentials, mock_dataset):
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        types = helper.get_resource_types_by_service_id("service1")
        assert types == ["type1", "type2"]
        assert helper.get_resource_types_by_service_id("notfound") == []


def test_slack_get_support_services_filter_by_prefix(mock_credentials, mock_dataset):
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        filtered = helper.slack_get_support_services_filter_by_prefix("Comp")
        assert "Compute" in filtered
        assert filtered["Compute"][0]["displayName"] == "VM"
        filtered2 = helper.slack_get_support_services_filter_by_prefix("Blob")
        assert "Storage" in filtered2


def test_get_name_strip_invalid_chars(mock_credentials, mock_dataset):
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        assert helper._get_name_strip_invalid_chars("John!@# Doe$%^") == "John Doe"


@patch("azure_support.MicrosoftSupport")
def test_get_problem_classification_details(mock_ms, mock_credentials, mock_dataset):
    mock_pc = mock_credentials()
    mock_pc.id = "/providers/Microsoft.Support/services/service1/problemClassifications/pc1"
    mock_pc.display_name = "Problem 1"
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        with patch.object(helper, "get_problem_classifications_list", return_value=[mock_pc]):
            details = helper.get_problem_classification_details("subid", "service1", "pc1")
            assert details["id"] == mock_pc.id
            assert details["display_name"] == "Problem 1"


def test_get_support_ticket_azure_portal_url(mock_credentials, mock_dataset):
    with patch("azure_support.AzureSupportHelper._load_dataset_services_mapped", return_value=mock_dataset):
        helper = AzureSupportHelper(mock_credentials)
        url = helper._get_support_ticket_azure_portal_url("ticket/with/slash")
        assert "ticket%2Fwith%2Fslash" in url
