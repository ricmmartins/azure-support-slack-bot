import concurrent.futures
import hashlib
import json
import time
import re
import urllib.parse
import logging
import threading

from cachetools import cached, TTLCache
from collections import OrderedDict
from azure.identity import ChainedTokenCredential
from azure.mgmt.support import MicrosoftSupport
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.resource import SubscriptionClient
from concurrent.futures import ThreadPoolExecutor
from azure.mgmt.support.models import (
    SupportTicketDetails, ContactProfile, TechnicalTicketDetails
)


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dataset_services_mapped_path = 'data/dataset_services_mapped.json'


class AzureSupportHelper:

    HASH_CACHE_SIZE = 2048
    SERVICE_ARN_TEMPLATE = '/providers/Microsoft.Support/services/{sid}'
    PROBLEM_CLASSIFICATIONS_ARN_TEMPLATE = '/providers/Microsoft.Support/services/{sid}/problemClassifications/{pcid}'

    def __init__(self, credentials: ChainedTokenCredential):
        self.credentials = credentials
        self.subscription_client = SubscriptionClient(credentials)
        self.dataset = self._load_dataset_services_mapped(dataset_services_mapped_path)
        self.sub_list = []
        self.hash_cache = OrderedDict()

        threading.Thread(target=self._preload_get_subscription_list, daemon=True).start()

    def _load_dataset_services_mapped(self, filepath):
        with open(filepath, "r") as f:
            data = json.load(f)

            for group, services in data.items():
                # Optimize to reduce bite transfer over wire
                for s in services:
                    s['id'] = s['id'].split('/')[-1]

            return data

    def string_to_hash(self, value):
        # Primary use: since slack select options are limited to 75 chars,
        # but the azure resourse could theoreticly much longer as resource
        # group can be up to 64 char and resource name itself 64 also, plus
        # other string inthe resource id, this should offer safe-ish
        # work around to hash the resource id.
        hash = hashlib.sha256(value.encode('utf-8')).hexdigest()

        self.hash_cache[hash] = value
        self.hash_cache.move_to_end(hash)
        if len(self.hash_cache) > self.HASH_CACHE_SIZE:
            self.hash_cache.popitem(last=False)

        return hash

    @staticmethod
    @cached(cache=TTLCache(maxsize=1024, ttl=60 * 60 * 24))
    def get_problem_classifications_list(credentials, subscription_id, support_service_id):
        return list(
            MicrosoftSupport(credentials, subscription_id)
            .problem_classifications
            .list(support_service_id)
        )

    @staticmethod
    @cached(cache=TTLCache(maxsize=1024, ttl=60 * 60 * 24))
    def get_problem_classification(credentials, subscription_id, service_id, problem_classification_id):
        ms = MicrosoftSupport(credentials, subscription_id)
        return ms.problem_classifications.get(
            service_id, problem_classification_id)

    def get_problem_classification_details(self, subscription_id, support_service_id, problem_classification_id):
        # Try to find in cached list first
        for pc in self.get_problem_classifications_list(self.credentials, subscription_id, support_service_id):
            if pc.id.split('/')[-1] == problem_classification_id:
                return {'id': pc.id, 'display_name': pc.display_name}

        # If not found, use cached .get()
        pc = self.get_problem_classification(
            self.credentials, subscription_id, support_service_id, problem_classification_id)
        return {
            'id': pc.id,
            'display_name': pc.display_name
        }

    def slack_get_support_services_filter_by_prefix(self, prefix):
        prefix_lower = prefix.lower()
        filtered = {}

        for group, services in self.dataset.items():
            # If group name matches, include all services in this group
            if group.lower().startswith(prefix_lower):
                filtered[group] = services
            else:
                # Otherwise, filter services by displayName
                matched_services = [s for s in services if s.get("displayName", "").lower().startswith(prefix_lower)]
                if matched_services:
                    filtered[group] = matched_services

        return filtered

    def _preload_get_subscription_list(self):
        while True:
            sub_list = self.subscription_client.subscriptions.list()
            subs = []
            for group in list(sub_list):
                subs.append({
                    'id': group.subscription_id,
                    'display_name': group.display_name
                })

            self.sub_list = subs
            logger.info('preloading subscriptions completed')

            time.sleep(60 * 60)

    def get_subscription_list(self):
        return self.sub_list

    @staticmethod
    @cached(cache=TTLCache(maxsize=1024, ttl=60 * 60))
    def get_sub_resources_by_resource_type_concurrent(credentials, subscription_id, resource_type_list: tuple):

        def fetch_resources(resource_type):
            logger.info(f'resource_typeresource_type: {resource_type}')
            return list(
                ResourceManagementClient(credentials, subscription_id)
                .resources
                .list(filter=f"resourceType eq '{resource_type.lower()}'")
            )

        results = []
        with ThreadPoolExecutor() as executor:
            logger.info(f'resource_type_list: {resource_type_list}')
            future_to_type = {executor.submit(fetch_resources, rt): rt for rt in resource_type_list}
            for future in concurrent.futures.as_completed(future_to_type):
                rt = future_to_type[future]
                try:
                    resources = future.result()
                    logger.info(f'resourcesresources: {resources}')
                    results.extend(resources)
                except Exception as exc:
                    logger.info(f"Resource type {rt} generated an exception: {exc}")

        # Group by resource group
        grouped = {}
        for res in results:
            resource_group = res.id.split('resourceGroups/')[1].split('/providers')[0]
            try:
                grouped.setdefault(resource_group, []).append({
                    'id': res.id,
                    'name': res.name
                })
            except (ValueError, IndexError):
                # If resourceGroups not found or malformed id, skip
                continue

        logger.info(f'grouped: {grouped}')
        return grouped

    def get_resource_types_by_service_id(self, service_id):
        # Sub-optimum but simple
        for d in self.dataset:
            for r in self.dataset[d]:
                if service_id == r['id']:
                    return r['resourceTypes']

        return []

    def _get_name_strip_invalid_chars(self, st):
        return re.sub(r"[^A-Za-z\s\-']", "", st)

    def _get_support_ticket_azure_portal_url(self, ticket_id):
        encoded_id = urllib.parse.quote(ticket_id, safe='')
        return f"https://portal.azure.com/#view/Microsoft_Azure_Support/SupportRequestDetails.ReactView/id/{encoded_id}/portalJourney~/true"

    def submit_support_ticket(self, data):
        subscription_id = data['select_azure_subscription']

        service_id = data['select_azure_service']
        service_arn = self.SERVICE_ARN_TEMPLATE.format(sid=service_id)

        problem_classification_id = data['select_azure_service_problem_classifications']
        problem_classification_arn = self.PROBLEM_CLASSIFICATIONS_ARN_TEMPLATE.format(
            sid=service_id,
            pcid=problem_classification_id
        )

        resource_id = data.get('resource_id', None)

        title = data['subject']
        description = data['problem_details']
        severity = data['select_severity']

        # ContactProfile
        first_name = self._get_name_strip_invalid_chars(data['first_name'])
        last_name = self._get_name_strip_invalid_chars(data['last_name'])
        primary_email_address = data['section_contact_information_email']
        phone_number = data.get('select_preferred_contact_method_phone', None)
        additional_email_addresses = data.get('section_contact_information_additional_emails', None)
        preferred_contact_method = data['select_preferred_contact_method']
        advanced_diagnostic_consent = data['select_advanced_diagnostic_information']

        country = data.get('country', 'USA')
        preferred_time_zone = data.get('preferred_time_zone', 'Eastern Standard Time')  # https://support.microsoft.com/help/973627/microsoft-time-zone-index-values
        preferred_support_language = data.get('preferred_support_language', 'en-us')

        for d in data:
            logger.info(f"{d}: {data[d]}")

        ticket_details = SupportTicketDetails(
            title=title,
            description=description,
            severity=severity,
            service_id=service_arn,
            problem_classification_id=problem_classification_arn,
            contact_details=ContactProfile(
                first_name=first_name,
                last_name=last_name,
                primary_email_address=primary_email_address,
                phone_number=phone_number,
                country=country,
                preferred_contact_method=preferred_contact_method,
                preferred_time_zone=preferred_time_zone,  # https://support.microsoft.com/help/973627/microsoft-time-zone-index-values
                preferred_support_language=preferred_support_language,  # Preferred support language
                additional_email_addresses=additional_email_addresses
            ),
            advanced_diagnostic_consent=advanced_diagnostic_consent,
            technical_ticket_details=TechnicalTicketDetails(resource_id=resource_id) if resource_id else None
        )

        try:
            support_client = MicrosoftSupport(self.credentials, subscription_id)
            ticket_name = f"s{service_id}_{int(time.time())}"
            logger.info(f"Creating support ticket: {ticket_name} ...")
            support_ticket = support_client.support_tickets.begin_create(
                support_ticket_name=ticket_name,
                create_support_ticket_parameters=ticket_details
            )
            logger.info("Support ticket created successfully!")
            result = support_ticket.result()
            logger.info(f"Ticket ID: {result.id}")
            logger.info(f"Ticket Title: {result.title}")
            logger.info(f"Ticket Status: {result.status}")

            return {
                'success': True,
                'title': result.title,
                'url': self._get_support_ticket_azure_portal_url(result.id),
                'ticket_id': result.id,
                'status': result.status,
                'subscription_id': subscription_id
            }
        except Exception as e:
            logger.info(f"Failed to create support ticket: {str(e)}")
            return {'success': False}

    def get_resource_id_by_resource_hash(self, subscription_id, azure_service_id, resource_hash):
        # Optimize for second API call for same resource. LRU: move to end if accessed
        if resource_hash in self.hash_cache:
            self.hash_cache.move_to_end(resource_hash)
            return self.hash_cache[resource_hash]

        resource_types = self.get_resource_types_by_service_id(azure_service_id)
        logger.info(resource_types)
        resources = self.get_sub_resources_by_resource_type_concurrent(
            self.credentials, subscription_id, tuple(resource_types))

        logger.info(f'resources: {resources}')

        for rl in resources:
            for r in resources[rl]:
                rid = r['id']
                if AzureSupportHelper.string_to_hash(rid) == resource_hash:
                    return rid

        return None
