import json
import re

dataset_services_file_path = "data/dataset_services.json"


def map_dataset_services(filepath):
    with open(filepath, "r") as f:
        data = json.load(f)["value"]

    grouped = {}
    for service in data:
        props = service.get("properties", {})
        display_name = props.get("displayName")
        service_id = service.get("id")
        resourceTypes = props.get("resourceTypes", [])
        metadata = props.get("metadata", {})
        group_ids_str = metadata.get("groupIds", "").replace("ServiceGroup", "")

        # For readability, add empty space before each upper Latter.
        group_ids_str = re.sub(r'(?<!^)([A-Z])', r' \1', group_ids_str)

        # groupIds can be comma-separated, so split and strip
        group_ids = [gid.strip() for gid in group_ids_str.split(",") if gid.strip()]

        for group_id in group_ids:
            if ' - Preview' not in display_name:
                grouped.setdefault(group_id, []).append({
                    "id": service_id,
                    "displayName": display_name,
                    "resourceTypes": resourceTypes
                })

    # Sort groupIds and services within each group
    sorted_grouped = {}
    for group_id in sorted(grouped):
        sorted_grouped[group_id] = sorted(grouped[group_id], key=lambda x: x["displayName"].lower())

    print(json.dumps(sorted_grouped, indent=2))
    return sorted_grouped

# To dynamicly pull the data, make API call as per https://learn.microsoft.com/en-us/rest/api/support/services/list?view=rest-support-2024-04-01&tabs=HTTP#code-try-0
# run as:
# python ./generate_dataset_service_mapping.py > ./data/dataset_services_mapped.json


dataset_services_mapped = map_dataset_services(dataset_services_file_path)

# for i in sorted_grouped:
#     if i == 'Storage':
#         print(sorted_grouped[i])
