# Azure Service Health for Slack

A small Python service that posts Azure Service Health incidents to Slack.
Each alert creates one root message per subscription and tracking ID, then
updates that same message through Active, Updated, and Resolved states so
human replies stay in its thread. It is notification-only: it does not open
support requests or accept Slack commands.

## Architecture

The production deployment uses Azure Container Apps, Azure Table Storage,
Key Vault, a user-assigned managed identity, Azure Container Registry, and
workspace-based Application Insights. Azure Monitor sends Activity Log Alerts
with Common Alert Schema to `POST /api/service-health` through a Secure Webhook.
Container Apps Easy Auth validates the Entra token and the application also
requires the official AzNS caller application and
`ActionGroupsSecureWebhook` app role.

Slack is outbound only: the app calls `chat.postMessage` and `chat.update`
with a bot token that has the `chat:write` scope.

## Prerequisites

- Python 3.13 or Docker
- A Slack app created from `slack_app_manifest.yaml`
- Azure CLI and Azure Developer CLI (`azd`)
- An Azure subscription where you can create the resources in `infra/`
- `Application Administrator` while running the Secure Webhook setup script

The bot must be invited to every configured destination channel. The manifest
intentionally does not request `chat:write.public`.

## Local development

1. Copy `.env-example` to `.env`.
2. Set the Slack bot token, a Table endpoint accessible through
   `DefaultAzureCredential`, and a routing file or inline routing JSON.
3. Install and run:

```sh
pip install -r requirements.txt
python app.py
```

Send a sample Common Alert Schema payload to
`http://localhost:5000/api/service-health`. `APP_ENV=development` bypasses
Easy Auth only for local use; never deploy a production instance with that
value. When `APP_ENV` is unset the app assumes `production` and requires Easy
Auth plus `SERVICE_HEALTH_EXPECTED_AUDIENCE`.

Build the production image with `docker build -t azure-service-health-slack .`.
The image installs from public PyPI by default. Pass
`--build-arg PIP_INDEX_URL=<mirror>` when your network requires an internal
package mirror.

### Configuration

| Variable | Required | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Slack bot token with `chat:write` |
| `APP_ENV` | No | `production` (default), `test`, or `development` |
| `AZURE_TABLE_ENDPOINT` | Yes | Table endpoint for incident state |
| `SERVICE_HEALTH_ROUTES_JSON` / `SERVICE_HEALTH_ROUTES_FILE` | Yes | Channel routing |
| `SERVICE_HEALTH_EXPECTED_AUDIENCE` | Production | Comma-separated accepted token audiences (`api://<client-id>,<client-id>`) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | No | Enables Azure Monitor telemetry |
| `LOG_LEVEL` | No | Python log level (default `INFO`) |

Routes:

| Route | Purpose |
|---|---|
| `POST /api/service-health` | Authenticated Common Alert Schema webhook |
| `GET /healthz` | Process liveness |
| `GET /readyz` | Service Health configuration readiness |

## Service Health routing

Set either `SERVICE_HEALTH_ROUTES_JSON` or `SERVICE_HEALTH_ROUTES_FILE`. See
`config/service_health_routes.example.json`. `default_channel_id` is required.
Rules may filter by `subscription_ids`, `services`, and `regions`. All supplied
filters must match; highest priority wins, then greatest specificity, then file
order. The first selected channel is stored with the incident and remains fixed.

The incident key is the normalized subscription ID plus a SHA-256 hash of the
tracking ID. Azure Table ETags and a short lease coordinate replicas. Identical
retries and stale updates return 200 without calling Slack. Transient Slack or
Storage failures return 503 so Azure Monitor can retry; invalid payloads and
permanent Slack configuration errors return 4xx.

## Deploy with AZD

No deployment is performed automatically by this repository. Configure an AZD
environment before provisioning:

```sh
az login
azd auth login
azd env new
azd env set SLACK_BOT_TOKEN "<xoxb-token>"
azd env set SERVICE_HEALTH_ROUTES_JSON '{"default_channel_id":"C0123456789","rules":[]}'
azd provision
azd deploy
azd provision
```

The first `azd provision` creates the Container App with a public placeholder
image and no health probes. `azd deploy` builds and pushes the real image and
records `SERVICE_APP_IMAGE_NAME`; the second `azd provision` keeps that image
and enables the `/healthz` and `/readyz` probes. Run that second provision after
every first-time environment setup.

Secure Webhook tokens are Entra v2 access tokens whose `aud` claim is the API
client ID, so the deployment accepts both `api://<client-id>` and
`<client-id>` as audiences.

The pre-provision hook runs `scripts/configure-secure-webhook.ps1`. It creates
or reuses the protected API app registration, app role, API service principal,
and AzNS app-role assignment, then writes the resulting IDs to the AZD
environment. The script is idempotent and requires Microsoft Graph application
administration permission.
Azure CLI and AZD maintain separate authentication sessions, so both
`az login` and `azd auth login` are required on a clean workstation.

`infra/modules/service-health-alert.bicep` is deliberately isolated so the
Activity Log Alert can be repeated for additional subscriptions while the
Container App remains central. Deploy that module at the target subscription
with the central webhook and Secure Webhook app values.

Secrets are copied from AZD environment parameters into Key Vault during
provisioning and exposed to the Container App only as Key Vault secret
references. Runtime access to Key Vault and Table Storage uses the user-assigned
managed identity and RBAC.

## Operations

Application Insights receives requests, dependencies, exceptions, logs, and
custom counters. Useful starting queries:

```kusto
AppRequests
| where Name has "/api/service-health"
| summarize count(), percentile(DurationMs, 95) by ResultCode

AppTraces
| where Message has "Service Health"
| project TimeGenerated, SeverityLevel, Message, Properties

AppDependencies
| where Target has_any ("slack.com", "table.core.windows.net")
| summarize count(), failures=countif(Success == false) by Target, ResultCode
```

Alert on sustained webhook 503s, Slack or Table dependency failures, and no
successful webhook requests when incidents are expected. For a permanent
Slack error, verify the configured channel IDs and bot membership.

There is an unavoidable MVP crash window after a successful first
`chat.postMessage` and before `messageTs` is persisted. Exactly-once creation
would require a transactional queue, which is intentionally out of scope.
Reconcile by finding the message using its tracking ID, updating the Table
entity, and then replaying the alert. If a tracked Slack message is deleted,
the next update posts a new root message and stores its timestamp.

## Security notes

- Container Apps strips client-supplied `X-MS-CLIENT-PRINCIPAL*` headers, and
  the app additionally checks the AzNS caller app ID, app role, and audience.
- Easy Auth runs in `AllowAnonymous` mode so the `/healthz` and `/readyz`
  probes stay reachable; the webhook handler rejects any request without a
  validated AzNS principal.
- The managed identity has no Azure Support or subscription-wide roles; it
  only reads its Key Vault secrets and uses its own Storage account.
- Key Vault has purge protection enabled; `azd down` leaves a soft-deleted
  vault that blocks reusing the same name for the retention period.
- Key Vault and Storage keep public network access with RBAC-only data plane
  access. Add private endpoints if your policy requires network isolation.

## Tests

```sh
pip install -r requirements-test.txt
pytest
flake8 .
```

## License

This project is licensed under the [MIT License](LICENSE).
