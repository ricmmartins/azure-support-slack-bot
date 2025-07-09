import logging

from cachetools import cached, TTLCache
from azure.identity import ChainedTokenCredential
from azure_support import AzureSupportHelper
from slack_sdk import WebClient
from concurrent.futures import ThreadPoolExecutor
from helpers import Blocks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class OptionsHandler:

    def __init__(self, credentials: ChainedTokenCredential, azure_support: AzureSupportHelper):
        self.credentials = credentials
        self.azure_support = azure_support

    def get_select_azure_sub(self, user_input):
        input_list = self.azure_support.get_subscription_list()
        options = []
        for il in input_list:
            options.append(
                {"text": {"type": "plain_text", "text": il['display_name']}, "value": il['id']}
            )
        return options

    def get_select_azure_service(self, user_input, private_metadata):
        data_option_groups = self.azure_support.slack_get_support_services_filter_by_prefix(user_input)
        option_groups = []
        for dog in data_option_groups:
            options = []
            for option in data_option_groups[dog]:
                options.append(
                    {"text": {"type": "plain_text", "text": option['displayName']}, "value": option['id']}
                )
            option_groups.append({
                "label": {
                    "type": "plain_text",
                    "text": dog
                },
                "options": options
            })
        return option_groups

    @cached(cache=TTLCache(maxsize=1024, ttl=60 * 60))
    def get_select_azure_subscription_resources(self, subscription_id, select_azure_service_id):
        resource_types = self.azure_support.get_resource_types_by_service_id(select_azure_service_id)
        logger.debug(f'Resource types for service {select_azure_service_id}: {resource_types}')
        data_option_groups = self.azure_support.get_sub_resources_by_resource_type_concurrent(
            self.credentials, subscription_id, tuple(resource_types)
        )

        return data_option_groups

    def get_select_azure_subscription_resources_mapped(self, private_metadata):
        subscription_id = private_metadata['select_azure_subscription']
        select_azure_service_id = private_metadata['select_azure_service']

        data_option_groups = self.get_select_azure_subscription_resources(subscription_id, select_azure_service_id)
        logger.debug('Fetched sub-resources for Azure subscription')
        option_groups = []
        for dog in data_option_groups:
            options = []
            for option in data_option_groups[dog]:
                options.append({
                    "text": {"type": "plain_text", "text": option['name']},
                    "value": self.azure_support.string_to_hash(option['id'])
                })
            option_groups.append({
                "label": {
                    "type": "plain_text",
                    "text": dog
                },
                "options": options
            })
        options = [{"text": {"type": "plain_text", "text": "General question"}, "value": 'none'}]
        option_groups.append({
            "label": {
                "type": "plain_text",
                "text": "General question / Resource not available"
            },
            "options": options
        })
        logger.debug(f'Option groups for resources: {option_groups}')
        return option_groups

    def get_problem_classifications_options(self, subscription_id, support_service_id):
        problem_classifications = self.azure_support.get_problem_classifications_list(
            self.credentials, subscription_id, support_service_id)

        option_groups = {}
        options = []
        for pc in problem_classifications:
            if " / " in pc.display_name:
                group, value = pc.display_name.split(" / ", 1)
                group = group.strip()
                value = value.strip()
                option = {
                    'id': pc.id.split('/')[-1],
                    'display_name': value
                }
                option_groups.setdefault(group, []).append(option)
            else:
                options.append({
                    'id': pc.id.split('/')[-1],
                    'display_name': pc.display_name
                })

        if option_groups:
            return {"type": "option_groups", "values": option_groups}

        return {"type": "options", "values": options}

    def get_select_azure_service_problem_classifications(self, private_metadata):
        subscription_id = private_metadata['select_azure_subscription']
        select_azure_service_id = private_metadata['select_azure_service']
        input_data = self.get_problem_classifications_options(subscription_id, select_azure_service_id)
        option_type = input_data['type']
        input_list = input_data['values']
        response = None
        if option_type == 'options':
            options = []
            for il in input_list:
                options.append(
                    {"text": {
                        "type": "plain_text", "text": il['display_name'][:75]},
                        "value": il['id']
                     })
            response = options
        else:
            option_groups = []
            for dog in input_list:
                options = []
                for option in input_list[dog]:
                    options.append(
                        {"text": {"type": "plain_text", "text": option['display_name'][:75]}, "value": option['id']}
                    )
                option_groups.append({
                    "label": {
                        "type": "plain_text",
                        "text": dog[:75]
                    },
                    "options": options
                })
            response = option_groups
        logger.debug(f'Problem classifications response: {response}')
        return {
            'type': option_type,
            'values': response,
        }


class SupportTicketSubmissionHandler:

    def __init__(self, submitted_data_as_dict, private_metadata,
                 azure_support: AzureSupportHelper, client: WebClient, executor: ThreadPoolExecutor):
        self.data = submitted_data_as_dict
        self.private_metadata = private_metadata
        self.azure_support = azure_support
        self.client = client
        self.executor = executor

    def handle(self):
        logger.debug(f'Flat data for support: {self.data}')
        logger.debug(f'Private metadata: {self.private_metadata}')

        # Prepare resource_id
        try:
            subscription_id = self.data[Blocks.AZURE_SUBSCRIPTION]
            azure_service_id = self.data[Blocks.AZURE_SERVICE]
            resource_hash = self.data[Blocks.AZURE_RESOURCE]
            self.data['resource_id'] = self.azure_support.get_resource_id_by_resource_hash(
                subscription_id, azure_service_id, resource_hash
            )
        except Exception as e:
            logger.exception(f"Failed to get resource_id: {e}")
            self._send_slack_error(self.private_metadata, "Failed to get resource ID.")
            return

        self.executor.submit(self._submit_support_ticket, self.data, self.private_metadata)
        logger.info('Support ticket submission task submitted')

    def _send_slack_error(self, text):
        channel_id = self.private_metadata.get('channel_select_block')
        user_id = self.private_metadata.get('user_id')
        channel = channel_id if channel_id else user_id
        if channel_id:
            text = text.replace('Hello!', f'Hello <@{user_id}>!')
        slack_data = {
            'channel': channel,
            'blocks': [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        }
        self._handle_slack_post_msg('channel', slack_data)

    def _submit_support_ticket(self, data, private_metadata):
        try:
            logger.info('Submitting Azure support ticket...')
            res = self.azure_support.submit_support_ticket(data)
            if res['success']:
                self._notify_slack_success(res)
            else:
                logger.warning('Azure support ticket creation failed')
                self._send_slack_error(private_metadata, "Hello! We had some trouble creating the support ticket.")
            logger.info('Support ticket submission finished')
        except Exception as e:
            logger.exception(f"Exception in submit_support_ticket: {e}")
            self._send_slack_error(private_metadata, "Hello! We had some trouble creating the support ticket.")

    def _notify_slack_success(self, res):
        channel_id = self.data.get('channel_select_block')
        user_id = self.private_metadata.get('user_id')
        channel = channel_id if channel_id else user_id
        text = (
            "Hello! Your support request has been successfully processed. "
            "Additional information can be found in the thread."
        )
        if channel_id:
            text = text.replace('Hello!', f'Hello <@{user_id}>!')
        slack_data = {
            'channel': channel,
            'blocks': [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        }
        thread_ts = self._handle_slack_post_msg('channel', slack_data)

        msg_data = (
            "Subject: {subject}\n"
            "Problem details: {problem_details}\n\n"
            "Subscription: {subscription_text} ({subscription})\n"
            "Azure service: {service_text}\n"
            "Azure Service Problem Type: {problem_type_text}\n"
            "Azure resource: {resource_text}\n"
            "Severity: {severity_text}\n"
        ).format(
            subject=self.data.get(Blocks.SUBJECT, ''),
            problem_details=self.data.get(Blocks.PROBLEM_DETAILS, ''),
            subscription_text=self.data.get(Blocks.AZURE_SUBSCRIPTION_TEXT, ''),
            subscription=self.data.get(Blocks.AZURE_SUBSCRIPTION, ''),
            service_text=self.data.get(Blocks.AZURE_SERVICE_TEXT, ''),
            problem_type_text=self.data.get(Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_TEXT, ''),
            resource_text=self.data.get(Blocks.AZURE_RESOURCE_TEXT, 'N/A. General question'),
            severity_text=self.data.get(Blocks.SEVERITY_TEXT, '')
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Click <{res['url']}|here> to view your support ticket in Azure portal. "
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{msg_data}```"
                }
            }
        ]
        slack_data['thread_ts'] = thread_ts
        slack_data['blocks'] = blocks
        self._handle_slack_post_msg('thread', slack_data)

    def _handle_slack_post_msg(self, msg_type, slack_data):
        try:
            if msg_type == 'channel':
                res = self.client.chat_postMessage(
                    text=".",
                    channel=slack_data['channel'],
                    blocks=slack_data['blocks']
                )
                return res['ts']
            elif msg_type == 'thread':
                self.client.chat_postMessage(
                    text=".",
                    channel=slack_data['channel'],
                    blocks=slack_data['blocks'],
                    thread_ts=slack_data['thread_ts']
                )
        except Exception as e:
            logger.exception(f"Exception in _handle_slack_post_msg: {e}")
