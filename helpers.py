import json
import time
from functools import wraps
import logging

logger = logging.getLogger(__name__)


def timeit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        elapsed_ms = (end - start) * 1000
        logger.info(f"Function {func.__name__} took {elapsed_ms:.2f} ms")
        return result
    return wrapper


class Shortcuts:
    OPEN_AZURE_SUPPORT_TICKET = 'open_azure_support_ticket'


class Blocks:
    SUBJECT = 'subject'
    PROBLEM_DETAILS = 'problem_details'
    AZURE_RESOURCE_INFORMATION = 'section_azure_resource_information'
    AZURE_SUBSCRIPTION = 'select_azure_subscription'
    AZURE_SERVICE = 'select_azure_service'
    AZURE_SERVICE_PROBLEM_CLASSIFICATIONS = 'select_azure_service_problem_classifications'
    AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_DESCRIPTION = 'select_azure_service_problem_classifications_full_text'
    AZURE_RESOURCE = 'select_azure_resource'
    ADVANCED_DIAGNOSTIC_INFO = 'select_advanced_diagnostic_information'
    SEVERITY = 'select_severity'
    PREFERRED_CONTACT_METHOD = 'select_preferred_contact_method'
    CONTACT_INFO = 'section_contact_information'
    CHANNEL_TICKET_CONFIRMATION = 'select_msg_post_destination'
    PREFERRED_CONTACT_METHOD_PHONE = 'select_preferred_contact_method_phone'

    CLOSING_VIEW = 'closing_view'

    BLOCK_ID_CONTACT_INFO_FULL_NAME = 'section_contact_information_full_name'
    BLOCK_ID_CONTACT_INFO_EMAIL = 'section_contact_information_email'
    BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS = 'section_contact_information_additional_emails'

    AZURE_SUBSCRIPTION_TEXT = 'select_azure_subscription_text'
    AZURE_SERVICE_TEXT = 'select_azure_service_text'
    AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_TEXT = 'select_azure_service_problem_classifications_text'
    AZURE_RESOURCE_TEXT = 'select_azure_resource_text'
    SEVERITY_TEXT = 'select_severity_text'


class BlockLoader:

    FOLDER_PATH = './slack_blocks'

    @staticmethod
    def get_block(file_name, block_index=None):
        with open(f'{BlockLoader.FOLDER_PATH}/{file_name}.json') as f:
            blocks = json.load(f)['blocks']
            return blocks[block_index] if block_index is not None else blocks

    @staticmethod
    def get_blocks(*file_name_args):
        blocks = []
        for file_name in list(file_name_args):
            blocks.extend(BlockLoader.get_block(file_name))

        return blocks

    @staticmethod
    def get_block_exp(path, block_id, action_id):
        with open(path) as f:
            blocks = json.load(f)

            for block in blocks['blocks']:
                block['block_id'] = block_id

                if block['type'] == 'input':
                    block['element']['action_id'] = action_id
                else:
                    logger.info(f'NOT MAPPED: {block["type"]}')

            return blocks['blocks']
