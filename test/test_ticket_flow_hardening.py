"""Regression tests for Slack ticket flow issues found during review."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app
from azure_support import AzureSupportHelper
from handlers import (
    SLACK_MAX_OPTIONS,
    SLACK_OPTION_TEXT_LIMIT,
    OptionsHandler,
    SupportTicketSubmissionHandler,
)

Blocks = app.Blocks


# --- Azure Support helper -------------------------------------------------

@pytest.fixture
def helper():
    with patch("threading.Thread"), patch(
            "azure_support.AzureSupportHelper._load_dataset_services_mapped",
            return_value={"Compute": [{
                "id": "svc1", "displayName": "VM",
                "resourceTypes": ["Microsoft.Compute/virtualMachines"],
            }]}):
        yield AzureSupportHelper(MagicMock())


def test_general_question_resource_resolves_to_none(helper):
    assert helper.get_resource_id_by_resource_hash("sub", "svc1", "none") is None
    assert helper.get_resource_id_by_resource_hash("sub", "svc1", "") is None


def test_resource_hash_lookup_resolves_uncached_resource(helper):
    rid = "/subscriptions/s/resourceGroups/rg/providers/x/y/vm1"
    resource_hash = helper.string_to_hash(rid)
    helper.hash_cache.clear()
    with patch.object(
            AzureSupportHelper, "get_sub_resources_by_resource_type_concurrent",
            return_value={"rg": [{"id": rid, "name": "vm1"}]}):
        assert helper.get_resource_id_by_resource_hash(
            "sub", "svc1", resource_hash) == rid


def test_resources_are_grouped_case_insensitively_and_bad_ids_skipped():
    resources = [
        SimpleNamespace(id="/subscriptions/s/resourceGroups/RG1/providers/a/b/one", name="one"),
        SimpleNamespace(id="/subscriptions/s/resourcegroups/rg2/providers/a/b/two", name="two"),
        SimpleNamespace(id="/subscriptions/s/providers/a/b/three", name="three"),
        SimpleNamespace(id=None, name="four"),
    ]
    with patch("azure_support.ResourceManagementClient") as rmc:
        rmc.return_value.resources.list.return_value = resources
        grouped = AzureSupportHelper.get_sub_resources_by_resource_type_concurrent(
            object(), "sub-grouping-test", ("a/b",))
    assert set(grouped) == {"RG1", "rg2"}
    assert grouped["RG1"][0]["name"] == "one"


def _ticket_data():
    return {
        "select_azure_subscription": "sub",
        "select_azure_service": "svc",
        "select_azure_service_problem_classifications": "pc",
        "subject": "Subject",
        "problem_details": "Details",
        "select_severity": "c",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "section_contact_information_email": "ada@example.com",
        "select_preferred_contact_method": "email",
        "select_advanced_diagnostic_information": "Yes",
    }


def test_ticket_uses_unique_name_and_configurable_contact_defaults(helper, monkeypatch):
    monkeypatch.setenv("SUPPORT_CONTACT_COUNTRY", "BRA")
    monkeypatch.setenv("SUPPORT_PREFERRED_TIME_ZONE", "E. South America Standard Time")
    monkeypatch.setenv("SUPPORT_PREFERRED_LANGUAGE", "pt-br")
    with patch("azure_support.MicrosoftSupport") as support:
        create = support.return_value.support_tickets.begin_create
        create.return_value.result.return_value = SimpleNamespace(
            title="Subject", id="/tickets/1", status="Open")
        assert helper.submit_support_ticket(_ticket_data())["success"]
        assert helper.submit_support_ticket(_ticket_data())["success"]

    first, second = [c.kwargs for c in create.call_args_list]
    assert first["support_ticket_name"].startswith("slack-")
    assert first["support_ticket_name"] != second["support_ticket_name"]
    contact = first["create_support_ticket_parameters"].contact_details
    assert contact.country == "BRA"
    assert contact.preferred_time_zone == "E. South America Standard Time"
    assert contact.preferred_support_language == "pt-br"


# --- Option builders ------------------------------------------------------

def test_subscription_options_are_filtered_truncated_and_capped():
    support = MagicMock()
    support.get_subscription_list.return_value = [
        {"id": f"sub{i}", "display_name": f"Prod {'x' * 100} {i}"}
        for i in range(150)
    ] + [{"id": "dev", "display_name": "Dev"}]
    handler = OptionsHandler(MagicMock(), support)

    options = handler.get_select_azure_sub("")
    assert len(options) == SLACK_MAX_OPTIONS
    assert all(len(o["text"]["text"]) <= SLACK_OPTION_TEXT_LIMIT for o in options)
    assert [o["value"] for o in handler.get_select_azure_sub("dev")] == ["dev"]


def test_resource_options_keep_room_for_general_question():
    support = MagicMock()
    support.get_resource_types_by_service_id.return_value = ["t"]
    support.get_sub_resources_by_resource_type_concurrent.return_value = {
        f"rg{g}": [{"id": f"id{g}-{i}", "name": f"res{i}"} for i in range(30)]
        for g in range(5)
    }
    support.string_to_hash.side_effect = lambda value: value
    handler = OptionsHandler(MagicMock(), support)

    groups = handler.get_select_azure_subscription_resources_mapped({
        "select_azure_subscription": "sub", "select_azure_service": "svc"})
    total = sum(len(group["options"]) for group in groups)
    assert total <= SLACK_MAX_OPTIONS
    assert groups[-1]["label"]["text"].startswith("General question")


def test_resources_beyond_cap_are_reachable_by_typing():
    support = MagicMock()
    support.get_resource_types_by_service_id.return_value = ["t"]
    support.get_sub_resources_by_resource_type_concurrent.return_value = {
        "rg": [{"id": f"id{i}", "name": f"vm-{i:03d}"} for i in range(250)]}
    support.string_to_hash.side_effect = lambda value: value
    handler = OptionsHandler(MagicMock(), support)
    metadata = {"select_azure_subscription": "sub", "select_azure_service": "svc"}

    groups = handler.get_select_azure_subscription_resources_mapped(metadata, "VM-24")
    names = [o["text"]["text"] for g in groups[:-1] for o in g["options"]]
    assert names == ["vm-240", "vm-241", "vm-242", "vm-243", "vm-244",
                     "vm-245", "vm-246", "vm-247", "vm-248", "vm-249"]
    assert groups[-1]["label"]["text"].startswith("General question")


def test_problem_classifications_are_filtered_by_query():
    support = MagicMock()
    support.get_problem_classifications_list.return_value = [
        MagicMock(id=f"x/pc{i}", display_name=f"Group / Problem {i}") for i in range(150)
    ]
    handler = OptionsHandler(MagicMock(), support)
    result = handler.get_select_azure_service_problem_classifications(
        {"select_azure_subscription": "sub-pc-filter", "select_azure_service": "svc"},
        "problem 149")
    assert [o["value"] for g in result["values"] for o in g["options"]] == ["pc149"]


# --- Submission handler error paths ---------------------------------------

def _submission(support, data=None):
    slack = MagicMock()
    executor = MagicMock()
    executor.submit.side_effect = lambda fn, *args: fn(*args)
    handler = SupportTicketSubmissionHandler(
        data or {
            Blocks.AZURE_SUBSCRIPTION: "sub",
            Blocks.AZURE_SERVICE: "svc",
            Blocks.AZURE_RESOURCE: "hash",
            "channel_select_block": "CCHANNEL",
        },
        {"user_id": "U1"}, support, slack, executor)
    return handler, slack


def test_resource_lookup_failure_notifies_slack():
    support = MagicMock()
    support.get_resource_id_by_resource_hash.side_effect = RuntimeError("boom")
    handler, slack = _submission(support)
    handler.handle()
    kwargs = slack.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "CCHANNEL"
    assert "<@U1>" in kwargs["blocks"][0]["text"]["text"]
    support.submit_support_ticket.assert_not_called()


def test_failed_ticket_submission_notifies_user_directly():
    support = MagicMock()
    support.get_resource_id_by_resource_hash.return_value = None
    support.submit_support_ticket.return_value = {"success": False}
    handler, slack = _submission(support, {
        Blocks.AZURE_SUBSCRIPTION: "sub",
        Blocks.AZURE_SERVICE: "svc",
        Blocks.AZURE_RESOURCE: "none",
    })
    handler.handle()
    kwargs = slack.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "U1"
    assert "trouble creating" in kwargs["blocks"][0]["text"]["text"]


# --- Modal mapping and validation -----------------------------------------

def _valid_submission():
    data = {block_id: "x" for block_id in app.REQUIRED_SELECTIONS}
    data["last_name"] = "Lovelace"
    return data


def test_validate_submission_accepts_complete_form():
    assert app.validate_submission(_valid_submission()) == ({}, [])


def test_validate_submission_reports_field_errors_and_missing_selections():
    data = _valid_submission()
    data.pop(Blocks.SEVERITY)
    data["last_name"] = ""
    data[Blocks.PREFERRED_CONTACT_METHOD] = "phone"
    data[Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS] = ["ok@example.com", "not-an-email"]

    errors, missing = app.validate_submission(data)

    assert set(errors) == {
        Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS,
        Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME,
        Blocks.PREFERRED_CONTACT_METHOD_PHONE,
    }
    assert missing == ["Severity"]


def test_validate_submission_limits_additional_emails():
    data = _valid_submission()
    data[Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS] = [
        f"user{i}@example.com" for i in range(app.MAX_ADDITIONAL_EMAILS + 1)]
    errors, _ = app.validate_submission(data)
    assert Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS in errors


def test_map_submitted_data_handles_cleared_selects_and_email_lists():
    submitted = {
        Blocks.SEVERITY: {Blocks.SEVERITY: {"type": "static_select", "selected_option": None}},
        Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME: {
            Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME: {"value": None}},
        Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS: {
            Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS: {
                "value": "a@example.com; b@example.com,\nc@example.com"}},
    }
    result = app.map_submitted_data_to_flat_dict(submitted)
    assert Blocks.SEVERITY not in result
    assert result["first_name"] == "" and result["last_name"] == ""
    assert result[Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS] == [
        "a@example.com", "b@example.com", "c@example.com"]


def test_contact_information_omits_empty_initial_values():
    blocks = [{
        "block_id": Blocks.BLOCK_ID_CONTACT_INFO_EMAIL,
        "element": {"initial_value": "placeholder@example.com"},
    }]
    result = app.handle_contact_information(blocks, {"email": None})
    assert "initial_value" not in result[0]["element"]


def test_ticket_allowlist(monkeypatch):
    monkeypatch.delenv("SUPPORT_TICKET_ALLOWED_SLACK_USER_IDS", raising=False)
    assert app.is_user_allowed("UANY")
    monkeypatch.setenv("SUPPORT_TICKET_ALLOWED_SLACK_USER_IDS", " U1, U2 ,")
    assert app.is_user_allowed("U1")
    assert app.is_user_allowed("U2")
    assert not app.is_user_allowed("U3")
