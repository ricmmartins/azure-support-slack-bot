import app
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Ensure the parent directory is in sys.path so app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def test_handle_contact_information_sets_initial_values():
    blocks = [
        {'block_id': app.Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME, 'element': {}},
        {'block_id': app.Blocks.BLOCK_ID_CONTACT_INFO_EMAIL, 'element': {}}
    ]
    private_metadata = {'real_name': 'John Doe', 'email': 'john@example.com'}
    result = app.handle_contact_information(blocks, private_metadata)
    assert result[0]['element']['initial_value'] == 'John Doe'
    assert result[1]['element']['initial_value'] == 'john@example.com'


def test_get_as_json():
    data = {'a': 1}
    assert app.get_as_json(data) == '{"a": 1}'


def test_get_private_metadata():
    body = {"view": {"private_metadata": '{"foo": "bar"}'}}
    assert app.get_private_metadata(body) == {"foo": "bar"}


def test_update_private_metadata_from_action_selected_option():
    body = {'actions': [{'action_id': 'aid', 'selected_option': {'value': 'val'}}], 'view': {'private_metadata': '{}'}}
    result = app.update_private_metadata_from_action(body)
    assert result['aid'] == 'val'


def test_map_submitted_data_to_flat_dict_handles_full_name_and_emails():
    submitted_data = {
        app.Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME: {
            app.Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME: {'value': 'John Doe'}
        },
        app.Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS: {
            app.Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS: {'value': 'a@b.com,c@d.com'}
        }
    }
    result = app.map_submitted_data_to_flat_dict(submitted_data)
    assert result['first_name'] == 'John'
    assert result['last_name'] == 'Doe'
    assert result[app.Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS] == ['a@b.com', 'c@d.com']
