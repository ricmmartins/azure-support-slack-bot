import os
import json
import logging
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from azure_support import AzureSupportHelper
from azure.identity import DefaultAzureCredential
from slack_bolt import App
from handlers import OptionsHandler, SupportTicketSubmissionHandler
from helpers import Blocks, BlockLoader, Shortcuts

# Logger setup
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('azure').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=".env")

slack_bot_token = os.environ['SLACK_BOT_TOKEN']
client = WebClient(slack_bot_token)
app = App(
    token=slack_bot_token,
    signing_secret=os.environ["SLACK_SIGNING_SECRET"])

azure_credentials = DefaultAzureCredential()
azure_support = AzureSupportHelper(azure_credentials)
options_handler = OptionsHandler(azure_credentials, azure_support)

BOT_ID = client.auth_test()['user_id']
executor = ThreadPoolExecutor()


def handle_contact_information(blocks, private_metadata):
    if private_metadata:
        for i, b in enumerate(blocks):
            if b['block_id'] == Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME:
                blocks[i]['element']['initial_value'] = private_metadata['real_name']
            elif b['block_id'] == Blocks.BLOCK_ID_CONTACT_INFO_EMAIL:
                blocks[i]['element']['initial_value'] = private_metadata['email']
        logger.debug(f'Contact info blocks after update: {blocks}')
    return blocks


def get_as_json(data):
    return json.dumps(data)


def get_private_metadata(body) -> dict:
    return json.loads(body["view"].get("private_metadata", "{}"))


def update_private_metadata_from_action(body):
    private_metadata = get_private_metadata(body)
    action = body['actions'][0]

    # Needs improvement for more specific case handling, like user selects to
    # "clear selection"
    try:
        if 'selected_option' in action:
            private_metadata[action['action_id']] = action['selected_option']['value']
        elif 'value' in action:
            private_metadata[action['action_id']] = action['value']
        elif 'selected_channel' in action:
            private_metadata[action['action_id']] = action['selected_channel']
    except Exception as e:
        logger.exception(f"Exception in update_private_metadata_from_action: {e}")

    return private_metadata


def log_private_metadata(private_metadata, action):
    for pm in private_metadata:
        logger.info(f'{pm}: {private_metadata[pm]}')


def get_init_blocks(user_info=None):
    blocks = []

    blocks.extend(BlockLoader.get_blocks(
        Blocks.SUBJECT,
        Blocks.PROBLEM_DETAILS,
        Blocks.AZURE_RESOURCE_INFORMATION,
        Blocks.AZURE_SUBSCRIPTION,
        Blocks.AZURE_SERVICE,
        Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS,
        Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_DESCRIPTION,
        Blocks.AZURE_RESOURCE,
        Blocks.ADVANCED_DIAGNOSTIC_INFO,
        Blocks.SEVERITY,
        Blocks.PREFERRED_CONTACT_METHOD
    ))
    blocks.extend(
        handle_contact_information(
            BlockLoader.get_block(Blocks.CONTACT_INFO),
            user_info))
    blocks.extend(BlockLoader.get_block(Blocks.CHANNEL_TICKET_CONFIRMATION))

    return blocks


def get_user_info(user_id):
    user_info = client.users_info(user=user_id)
    logger.debug(f'Fetched user_info: {user_info}')
    profile = user_info['user']['profile']
    email = profile.get('email')
    real_name = profile.get('real_name')
    phone = profile.get('phone')

    if user_id is not None and user_id != BOT_ID:
        logger.info(f"Message from user_id: {user_id}")

    user_info = {
        'user_id': user_id,
        'real_name': real_name,
        'phone': phone,
        'email': email
    }
    logger.info(f'Parsed user_info: {user_info}')
    return user_info


def map_submitted_data_to_flat_dict(submitted_data):
    result = {}
    logger.info(f"Submitted data: {submitted_data}")

    for sd in submitted_data:
        if sd == Blocks.BLOCK_ID_CONTACT_INFO_FULL_NAME:
            value = submitted_data[sd][sd]['value'].split(' ')
            first_name = value[0]
            last_name = ' '.join(value[1:])
            result['first_name'] = first_name
            result['last_name'] = last_name
        elif sd == Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS:
            if 'value' in submitted_data[sd][sd]:
                result[Blocks.BLOCK_ID_CONTACT_INFO_ADDITIONAL_EMAILS] = [email.strip(
                ) for email in submitted_data[sd][sd]['value'].split(',') if email.strip()]
        else:
            if 'selected_channel' in submitted_data[sd][sd]:
                result[sd] = submitted_data[sd][sd]['selected_channel']
            elif 'selected_conversation' in submitted_data[sd][sd]:
                result[sd] = submitted_data[sd][sd]['selected_conversation']
            elif 'selected_option' in submitted_data[sd][sd]:
                result[sd] = submitted_data[sd][sd]['selected_option']['value']
                result[f'{sd}_text'] = submitted_data[sd][sd]['selected_option']['text']['text']
            else:
                result[sd] = submitted_data[sd][sd]['value']

    return result


def open_support_modal_common(trigger_id, user_id, logger_message):
    """Common function to open support modal for both shortcut and slash command"""
    try:
        user_info = get_user_info(user_id)
        private_metadata = user_info

        view = {
            "type": "modal",
            "private_metadata": get_as_json(private_metadata),
            "callback_id": Shortcuts.OPEN_AZURE_SUPPORT_TICKET,
            "title": {
                "type": "plain_text",
                "text": "Create a support request"},
            "submit": {
                "type": "plain_text",
                "text": "Submit"},
            "blocks": get_init_blocks(user_info)}
        
        client.views_open(trigger_id=trigger_id, view=view)
        logger.info(logger_message)
    except SlackApiError as e:
        logger.error(f"Failed to open modal: {e.response['error']}")


@app.shortcut(Shortcuts.OPEN_AZURE_SUPPORT_TICKET)
def open_support_modal(ack, body, client, logger):
    ack()
    user_id = body['user']['id']
    trigger_id = body["trigger_id"]
    open_support_modal_common(trigger_id, user_id, 'Opened modal for support request via shortcut')


# 🚨 CRITICAL FIX: Add the missing slash command handler
@app.command("/azure-support")
def handle_azure_support_command(ack, body, client, logger):
    ack()
    user_id = body['user_id']
    trigger_id = body["trigger_id"]
    open_support_modal_common(trigger_id, user_id, 'Opened modal for support request via slash command')


def preload_azure_resources(private_metadata):
    required_keys = [
        Blocks.AZURE_SUBSCRIPTION,
        Blocks.AZURE_SERVICE
    ]
    if all(k in private_metadata for k in required_keys):
        def azure_resource():
            subscription_id = private_metadata[Blocks.AZURE_SUBSCRIPTION]
            select_azure_service_id = private_metadata[Blocks.AZURE_SERVICE]
            options_handler.get_select_azure_subscription_resources_raw(
                subscription_id,
                select_azure_service_id)

        executor.submit(azure_resource)


@app.event("app_mention")
def handle_app_mention(event, say):
    user_id = event.get("user")
    text = event.get("text", "")
    channel_id = event.get("channel")

    logger.info(f"handle_message_events.text: {text}")

    # Only react if the message is exactly a mention to the bot (no extra text)
    bot_mention = f"<@{BOT_ID}>"
    if user_id != BOT_ID and text.strip() == bot_mention:
        try:
            client.reactions_add(
                name="eyes",
                channel=channel_id,
                timestamp=event["ts"],
            )
        except Exception as e:
            logger.exception(f"Exception in handle_message_events: {e}")

    command = text.split(' ', 1)[1].strip() if ' ' in text else ''
    logger.info(f"handle_message_events.command: {command}")

    # Placeholder, possible approach to handle for user to request status
    # update on a support ticket etc.
    if command == "help":
        say("Supported commands:\n• help - Show this message\n• status - Get the latest status")
    elif command == "status":
        say("Current status: All systems operational.")
    else:
        say("Sorry, I didn't understand that command. Type `help` to see available commands.")


@app.event("message")
def handle_dm(event, say):
    # Only react to direct messages to the bot
    if event.get("channel_type") == "im" and event.get("user") != BOT_ID:
        try:
            client.reactions_add(
                name="eyes",
                channel=event["channel"],
                timestamp=event["ts"],
            )
        except Exception as e:
            logger.exception(f"Exception in handle_direct_msg: {e}")


@app.view(Shortcuts.OPEN_AZURE_SUPPORT_TICKET)
def handle_view_submission(ack, body, client, logger):
    ack({
        "response_action": "update",
        "view": {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Accepted"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": BlockLoader.get_block(Blocks.CLOSING_VIEW)
        }
    })

    submitted_data = body["view"]["state"]["values"]
    private_metadata = get_private_metadata(body)
    logger.debug(f'Submitted data: {submitted_data}')
    logger.debug(f'Private metadata: {private_metadata}')

    data = map_submitted_data_to_flat_dict(submitted_data)
    SupportTicketSubmissionHandler(
        data, private_metadata, azure_support, client, executor
    ).handle()


@app.action(Blocks.PREFERRED_CONTACT_METHOD)
def handle_select_preferred_contact_method(ack, body, client, logger):
    ack()
    private_metadata = get_private_metadata(body)
    action = body['actions'][0]
    if action['selected_option']['value'] == 'phone':
        phone_block = BlockLoader.get_block(
            Blocks.PREFERRED_CONTACT_METHOD_PHONE, 0)
        phone_block['element']['initial_value'] = private_metadata.get(
            'phone', '')
        blocks = body["view"]['blocks']
        for i, b in enumerate(blocks):
            if b['block_id'] == Blocks.PREFERRED_CONTACT_METHOD:
                blocks.insert(i + 1, phone_block)
                break
    else:
        blocks = body["view"]['blocks']
        idx = 0
        for i, b in enumerate(blocks):
            if b['block_id'] == Blocks.PREFERRED_CONTACT_METHOD:
                idx = i + 1
                break
        if idx < len(blocks) and blocks[idx].get(
                'block_id') == Blocks.PREFERRED_CONTACT_METHOD_PHONE:
            blocks.pop(idx)

    push_update_view(body, private_metadata)


@app.action(Blocks.AZURE_SUBSCRIPTION)
def handle_select_azure_subscription(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    preload_azure_resources(private_metadata)
    push_update_view(body, private_metadata)


@app.action(Blocks.AZURE_SERVICE)
def handle_select_azure_service(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    preload_azure_resources(private_metadata)
    push_update_view(body, private_metadata)


def handle_select_azure_service_problem_classifications_full_text(details, body):
    blocks = body["view"]['blocks']

    plain_text = BlockLoader.get_block(
        Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_DESCRIPTION)
    plain_text[0]['text']['text'] = details['display_name']
    plain_text_block_id = plain_text[0].get(
        'block_id', Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS_DESCRIPTION)

    found = False
    for idx, block in enumerate(blocks):
        if block.get('block_id') == plain_text_block_id:
            blocks[idx] = plain_text[0]
            found = True
            break
    if not found:
        blocks.extend(plain_text)

    return body


@app.action(Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS)
def handle_select_azure_service_problem_classifications(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    subscription_id = private_metadata[Blocks.AZURE_SUBSCRIPTION]
    select_azure_service_id = private_metadata[Blocks.AZURE_SERVICE]
    select_azure_service_problem_classifications_id = private_metadata[
        Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS]

    details = azure_support.get_problem_classification_details(
        subscription_id,
        select_azure_service_id,
        select_azure_service_problem_classifications_id
    )

    body = handle_select_azure_service_problem_classifications_full_text(details, body)
    push_update_view(body, private_metadata)


@app.action(Blocks.AZURE_RESOURCE)
def handle_select_azure_resource(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    push_update_view(body, private_metadata)


@app.action(Blocks.SEVERITY)
def handle_select_severity(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    push_update_view(body, private_metadata)


@app.action(Blocks.ADVANCED_DIAGNOSTIC_INFO)
def handle_select_advanced_diagnostic_information(ack, body, client, logger):
    ack()
    private_metadata = update_private_metadata_from_action(body)
    push_update_view(body, private_metadata)


@app.options(Blocks.AZURE_SUBSCRIPTION)
def options_azure_subscription(ack, body):
    user_input = body.get("value", "")
    options = options_handler.get_select_azure_sub(user_input)
    ack(options=options)


def push_update_view(body, private_metadata):
    client.views_update(
        view_id=body["view"]["id"],
        hash=body["view"]["hash"],
        view={
            'private_metadata': get_as_json(private_metadata),
            "type": "modal",
            "callback_id": body["view"]["callback_id"],
            "title": {
                "type": "plain_text",
                "text": "Create a support request"},
            "submit": {
                "type": "plain_text",
                "text": "Submit"},
            "blocks": body["view"]['blocks']})


@app.options(Blocks.AZURE_SERVICE)
def options_azure_service(ack, body, client):
    user_input = body.get("value", "")
    private_metadata = get_private_metadata(body)
    log_private_metadata(private_metadata, Blocks.AZURE_SERVICE)

    og = options_handler.get_select_azure_service(user_input, private_metadata)
    ack(option_groups=og)


@app.options(Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS)
def options_azure_service_problem_classifications(ack, body):
    private_metadata = get_private_metadata(body)
    log_private_metadata(
        private_metadata,
        Blocks.AZURE_SERVICE_PROBLEM_CLASSIFICATIONS)

    # Check required keys and return empty options if missing
    if not private_metadata.get(Blocks.AZURE_SUBSCRIPTION) or not private_metadata.get(Blocks.AZURE_SERVICE):
        ack(options=[])
        return

    data = options_handler.get_select_azure_service_problem_classifications(
        private_metadata)
    ack(option_groups=data['values']) if data['type'] == 'option_groups' else ack(
        options=data['values'])


@app.options(Blocks.AZURE_RESOURCE)
def options_azure_resource(ack, body):
    private_metadata = get_private_metadata(body)
    option_groups = options_handler.get_select_azure_subscription_resources_mapped(private_metadata)
    ack(option_groups=option_groups)


if __name__ == "__main__":
    app.start(port=5000)
