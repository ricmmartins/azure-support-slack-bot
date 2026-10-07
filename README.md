# Azure Service Health for Slack

[![CI](https://github.com/ricmmartins/azure-support-slack-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ricmmartins/azure-support-slack-bot/actions/workflows/ci.yml)

This service posts [Azure Service Health](https://learn.microsoft.com/azure/service-health/overview)
events to Slack: service issues, planned maintenance, health advisories and
security advisories. Each incident gets **one** Slack message, and that
message is edited in place as the incident moves from Active to Updated to
Resolved, so the team's discussion stays in one thread.

The flow is one-way. Azure calls the service, and the service posts to Slack.
The Slack app has a single permission, `chat:write`. It has no slash commands
and receives nothing from Slack.

```mermaid
flowchart LR
    SH[Azure Service Health] --> ALA[Activity Log Alert<br/>one per subscription]
    ALA --> AG[Action Group<br/>Secure Webhook]
    AG -- "HTTPS + Entra token" --> ACA
    subgraph rg["Resource group rg-&lt;env&gt;"]
        ACA[Container App<br/>Easy Auth + Flask]
        KV[Key Vault<br/>Slack token]
        TS[Table Storage<br/>incident state]
        AI[Application Insights]
        ACR[Container Registry]
    end
    ACA -- managed identity --> KV
    ACA -- managed identity --> TS
    ACA --> AI
    ACR -- managed identity pull --> ACA
    ACA -- "chat.postMessage / chat.update" --> SL[Slack channels]
```

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Create the Slack app](#2-create-the-slack-app)
3. [Write the routing JSON](#3-write-the-routing-json)
4. [Deploy to Azure](#4-deploy-to-azure)
5. [Validate the deployment](#5-validate-the-deployment)
6. [Monitor more subscriptions](#6-monitor-more-subscriptions)
7. [Troubleshooting](#7-troubleshooting)
8. [Costs, removal and known limitations](#8-costs-removal-and-known-limitations)
9. [How it works](#how-it-works)
10. [Local development](#local-development)

Commands are shown for **bash** (macOS, Linux, WSL) and **PowerShell 7**
(Windows, macOS, Linux). Where they are the same, only one block is shown.

## 1. Prerequisites

### Tools

| Tool | Why | Install |
|---|---|---|
| Git | Clone this repository | [git-scm.com](https://git-scm.com/downloads) |
| Azure CLI (`az`) | Microsoft Graph calls and checks | [Install the Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure Developer CLI (`azd`) | Provisions and deploys everything | [Install azd](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) |
| PowerShell 7 (`pwsh`) | Runs the pre-provision hook on **every** OS, including macOS and Linux | [Install PowerShell](https://learn.microsoft.com/powershell/scripting/install/installing-powershell) |
| Python 3.11+ | Optional. Only for local development and tests | [python.org](https://www.python.org/downloads/) |
| Docker | Optional. The image is built in Azure Container Registry | [Get Docker](https://docs.docker.com/get-docker/) |

On Windows, use PowerShell 7 (`pwsh`), not Windows PowerShell 5.1. Check
your tools:

```sh
az version
azd version
pwsh --version
```

### Permissions

| Where | Role you need | Why |
|---|---|---|
| Microsoft Entra ID | [Application Administrator](https://learn.microsoft.com/entra/identity/role-based-access-control/permissions-reference#application-administrator) or Cloud Application Administrator | Create the app registration and app role that protect the webhook, and grant that role to Azure Monitor |
| Azure subscription | [Owner](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles), or Contributor plus User Access Administrator | Create resources and the role assignments for the managed identity |
| Slack workspace | Permission to install apps (some workspaces require admin approval) | Install the bot |

If you do not have the Entra role, see
[Graph permission errors](#graph-permission-errors) for how an admin can do
that single step for you.

### Clone the repository

```sh
git clone https://github.com/ricmmartins/azure-support-slack-bot.git
cd azure-support-slack-bot
```

## 2. Create the Slack app

1. Open <https://api.slack.com/apps> and select **Create New App** →
   **From a manifest**.
2. Pick your workspace, choose **YAML**, and paste the contents of
   [`slack_app_manifest.yaml`](slack_app_manifest.yaml). Select **Next** →
   **Create**.
3. Select **Install to Workspace** → **Allow**.
4. Open **OAuth & Permissions** and copy the **Bot User OAuth Token**. It
   starts with `xoxb-`. Treat it as a password.
5. Invite the bot to **every** channel it will post to, including the default
   channel. In each channel, send:

   ```text
   /invite @azure-service-health
   ```

   The bot can only post to channels it is a member of. This is intentional:
   the manifest does not request `chat:write.public`.

6. Get each channel ID. Open the channel, click its name at the top, and
   scroll to the bottom of the **About** tab. The ID looks like `C0123456789`.
   You can also right-click the channel → **Copy** → **Copy link**; the ID is
   the last part of the URL. Use IDs, not channel names.

## 3. Write the routing JSON

Routing decides which channel gets each incident. Create a file named
`routes.json` in the repository folder (it is only used to fill an azd
variable, so you do not need to commit it):

```json
{
  "default_channel_id": "C0000000000",
  "rules": [
    {
      "channel_id": "C1111111111",
      "priority": 100,
      "subscription_ids": ["00000000-0000-0000-0000-000000000000"]
    },
    {
      "channel_id": "C2222222222",
      "priority": 50,
      "services": ["Azure Kubernetes Service", "Virtual Machines"]
    },
    {
      "channel_id": "C3333333333",
      "priority": 50,
      "services": ["Azure Database for PostgreSQL flexible servers"],
      "regions": ["East US", "East US 2"]
    }
  ]
}
```

How rules work:

- `default_channel_id` is required. Incidents that match no rule go there.
- A rule can filter on `subscription_ids`, `services` and `regions`. Every
  filter you include must match; filters you leave out match anything.
  Matching is case-insensitive.
- Service and region names must match what Service Health publishes, for
  example `East US`, not `eastus`. Copy them from **Service Health** in the
  Azure portal.
- When several rules match, the highest `priority` wins (default `0`), then
  the rule with more filters, then the earlier rule in the file.
- An incident stays in the channel chosen for its first message, even if a
  later update would match a different rule.

The smallest valid configuration sends everything to one channel:

```json
{ "default_channel_id": "C0000000000", "rules": [] }
```

## 4. Deploy to Azure

### 4.1 Sign in to both CLIs

The Azure CLI and azd keep separate sign-ins. Use the **same account and
tenant** for both:

```sh
az login
azd auth login
```

If your account has access to several tenants, pass the tenant explicitly:
`az login --tenant <tenant-id>` and `azd auth login --tenant-id <tenant-id>`.

### 4.2 Create an azd environment

Pick a short environment name (it becomes part of resource names, for
example `rg-shh-prod`), the subscription ID and a region that supports
Azure Container Apps.

bash:

```bash
azd env new shh-prod \
  --subscription "<subscription-id>" \
  --location eastus2
```

PowerShell:

```powershell
azd env new shh-prod `
  --subscription "<subscription-id>" `
  --location eastus2
```

### 4.3 Set the two required values

| Variable | Value |
|---|---|
| `SLACK_BOT_TOKEN` | The `xoxb-` token from step 2. During provisioning it is written to Key Vault; the container only gets a Key Vault reference. |
| `SERVICE_HEALTH_ROUTES_JSON` | The routing JSON from step 3, on a single line. |

bash:

```bash
azd env set SLACK_BOT_TOKEN "xoxb-your-token"
azd env set SERVICE_HEALTH_ROUTES_JSON "$(tr -d '\r\n' < routes.json)"
```

PowerShell 7:

```powershell
azd env set SLACK_BOT_TOKEN "xoxb-your-token"
$routes = Get-Content routes.json -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 10 -Compress
azd env set SERVICE_HEALTH_ROUTES_JSON $routes
```

azd stores these values in `.azure/<env>/.env` on your machine. That folder
is git-ignored; do not commit it.

You do **not** set these yourself. The pre-provision hook writes them:

| Variable | Set by | Meaning |
|---|---|---|
| `AZURE_TENANT_ID` | hook | Tenant of the Azure CLI sign-in |
| `SERVICE_HEALTH_API_CLIENT_ID` | hook | Client ID of the app registration that protects the webhook |
| `SERVICE_HEALTH_API_OBJECT_ID` | hook | Object ID of that app registration |
| `SERVICE_HEALTH_API_IDENTIFIER_URI` | hook | `api://<client-id>` |
| `SERVICE_APP_IMAGE_NAME` | `azd deploy` | Image currently running, so re-provisioning keeps it |

### 4.4 Provision, deploy, provision

```sh
azd provision
azd deploy
azd provision
```

What each step does:

1. **`azd provision`** (first time) runs
   [`scripts/configure-secure-webhook.ps1`](scripts/configure-secure-webhook.ps1)
   as a pre-provision hook. The script creates or reuses the Entra app
   registration, adds the `ActionGroupsSecureWebhook` app role, and grants
   that role to the Azure Monitor service principal. azd then creates the
   resource group, Key Vault, Storage, Container Registry, Application
   Insights, the Container App, the Action Group and the Service Health alert.
   The Container App starts with a public placeholder image and **no health
   probes**, because your image does not exist yet.
2. **`azd deploy`** builds the image in Azure Container Registry, rolls it out
   to the Container App, and records the image name in `SERVICE_APP_IMAGE_NAME`.
3. **`azd provision`** (second time) re-applies the infrastructure with the
   real image and turns on the `/healthz` and `/readyz` probes. Without this
   step the app runs without probes.

You only need the double provision when you create an environment. Later,
use `azd deploy` for code changes and `azd provision` for configuration
changes (for example new routing JSON or a new Slack token). The hook runs on
every provision and does nothing if everything already exists.

The first provision usually takes 5–10 minutes.

## 5. Validate the deployment

Load the deployment outputs:

bash:

```bash
APP_URI=$(azd env get-value SERVICE_APP_URI)
RG=$(azd env get-value AZURE_RESOURCE_GROUP)
APP_NAME=$(azd env get-value SERVICE_APP_NAME)
ENV_NAME=$(azd env get-value AZURE_ENV_NAME)
```

PowerShell:

```powershell
$APP_URI = azd env get-value SERVICE_APP_URI
$RG = azd env get-value AZURE_RESOURCE_GROUP
$APP_NAME = azd env get-value SERVICE_APP_NAME
$ENV_NAME = azd env get-value AZURE_ENV_NAME
```

### 5.1 Health and readiness

bash:

```bash
curl -sS "$APP_URI/healthz"   # {"status":"healthy"}
curl -sS "$APP_URI/readyz"    # {"status":"ready"}
```

PowerShell:

```powershell
Invoke-RestMethod "$APP_URI/healthz"
Invoke-RestMethod "$APP_URI/readyz"
```

`/readyz` returns 503 when the configuration is invalid, for example broken
routing JSON. Check the logs (5.4) if that happens.

### 5.2 Confirm the webhook rejects anonymous callers

bash:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$APP_URI/api/service-health" \
  -H "Content-Type: application/json" \
  --data @docs/sample-service-health-alert.json
# Expected: 401
```

PowerShell:

```powershell
try {
  Invoke-RestMethod -Method Post -Uri "$APP_URI/api/service-health" `
    -ContentType application/json -InFile docs/sample-service-health-alert.json
} catch { $_.Exception.Response.StatusCode.value__ }   # Expected: 401
```

A 401 is the correct result: only Azure Monitor, with a valid Entra token,
can call the webhook.

### 5.3 Send a test notification through the Action Group

This sends a real, signed request from Azure Monitor to your service.

**Azure portal:** go to **Monitor** → **Alerts** → **Action groups**, open
`ag-<env>-service-health`, select **Test**, choose the sample type
**Service health alert**, and select **Test**. The result should show
success, and a message titled `TestActionGroup-TestServiceHealthAlert` should
appear in your default channel. See
[Test action groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#test-an-action-group-in-the-azure-portal)
and the [sample test payload](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-payload-samples#sample-test-action-service-health-alert).

**Azure CLI (alternative):**

bash:

```bash
az monitor action-group test-notifications create \
  --resource-group "$RG" \
  --action-group "ag-$ENV_NAME-service-health" \
  --alert-type servicehealth \
  --add-action webhook slack-service-health "$(azd env get-value SERVICE_HEALTH_WEBHOOK_URI)" \
    useaadauth "$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID)" \
    "$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)" usecommonalertschema
```

PowerShell:

```powershell
az monitor action-group test-notifications create `
  --resource-group $RG `
  --action-group "ag-$ENV_NAME-service-health" `
  --alert-type servicehealth `
  --add-action webhook slack-service-health (azd env get-value SERVICE_HEALTH_WEBHOOK_URI) `
    useaadauth (azd env get-value SERVICE_HEALTH_API_OBJECT_ID) `
    (azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI) usecommonalertschema
```

The test payload always uses the same tracking ID. If you run the test twice,
the second run returns `duplicate` and posts nothing new. That is the
deduplication working, not an error.

Right after the first deployment, role assignments can take a few minutes to
propagate. During that window the service returns 503 and Azure Monitor
retries automatically.

### 5.4 Logs and telemetry

Stream the container logs:

```sh
az containerapp logs show --name "$APP_NAME" --resource-group "$RG" --follow --tail 50
```

In PowerShell, use `$APP_NAME` and `$RG` the same way.

For history, open the Log Analytics workspace `log-<env>` in the portal →
**Logs**, and run:

```kusto
// Webhook calls by result
AppRequests
| where Url has "/api/service-health"
| summarize count(), p95=percentile(DurationMs, 95) by ResultCode, bin(TimeGenerated, 1h)

// What the service decided and why
AppTraces
| where Message has_any ("Service Health", "Rejected")
| project TimeGenerated, SeverityLevel, Message, Properties
| order by TimeGenerated desc

// Slack and Table Storage dependency failures
AppDependencies
| where Target has_any ("slack.com", "table.core.windows.net")
| summarize total=count(), failed=countif(Success == false) by Target, ResultCode
```

The service also emits two custom metrics: `service_health.requests`, split
by result (`created`, `updated`, `duplicate`, `stale`), and
`service_health.lifecycle`, split by lifecycle status. A good first alert is a sustained rate of 503 responses on
`/api/service-health`.

## 6. Monitor more subscriptions

The first deployment watches the subscription it was deployed to. To watch
another subscription in the **same Entra tenant**, deploy
[`infra/alert-subscription.bicep`](infra/alert-subscription.bicep) into it.
This creates the resource group `rg-<env>-service-health-alerts` with an
Action Group and a Service Health alert that point at the existing webhook.
You need Contributor (or Monitoring Contributor plus permission to create
resource groups) on the target subscription.

bash:

```bash
az deployment sub create \
  --subscription "<other-subscription-id>" \
  --name service-health-slack-alert \
  --location eastus2 \
  --template-file infra/alert-subscription.bicep \
  --parameters \
    environmentName="$(azd env get-value AZURE_ENV_NAME)" \
    webhookUri="$(azd env get-value SERVICE_HEALTH_WEBHOOK_URI)" \
    secureWebhookObjectId="$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID)" \
    secureWebhookIdentifierUri="$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)"
```

PowerShell:

```powershell
az deployment sub create `
  --subscription "<other-subscription-id>" `
  --name service-health-slack-alert `
  --location eastus2 `
  --template-file infra/alert-subscription.bicep `
  --parameters `
    environmentName=(azd env get-value AZURE_ENV_NAME) `
    webhookUri=(azd env get-value SERVICE_HEALTH_WEBHOOK_URI) `
    secureWebhookObjectId=(azd env get-value SERVICE_HEALTH_API_OBJECT_ID) `
    secureWebhookIdentifierUri=(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)
```

Repeat for each subscription, then add `subscription_ids` rules to your
routing JSON if you want per-subscription channels (`azd env set
SERVICE_HEALTH_ROUTES_JSON ...` and `azd provision`). For dozens of
subscriptions, consider
[deploying Service Health alerts with Azure Policy](https://learn.microsoft.com/azure/service-health/service-health-alert-deploy-policy)
using the same Action Group settings.

## 7. Troubleshooting

### `not_in_channel` or `channel_not_found`

The webhook returns `422 permanent_processing_failure`, and the logs show
`Permanent Service Health processing failure`. The bot is not a member of the
channel or the ID is wrong. Run `/invite @azure-service-health` in the channel
and confirm the ID (it starts with `C`, it is not the channel name). Azure
Monitor does not retry 4xx responses, so that event is not reposted. The next
update of the same incident creates the message.

### 401 from the Action Group

- `{"error":"authentication_required"}` means the request had no Entra
  identity. Make sure the Action Group webhook has **Enable the secure
  webhook** turned on. Running `azd provision` restores it.
- A 401 with no JSON body comes from Container Apps authentication: the token
  was rejected before reaching the app, usually because of an audience or
  issuer mismatch. Inspect it with
  `az containerapp auth show --name "$APP_NAME" --resource-group "$RG"`.

### 403 from the Action Group

The logs show `Rejected Service Health webhook identity: <reason>`:

| Reason | Fix |
|---|---|
| `caller application is not authorized` | The caller is not Azure Monitor (`461e8683-5575-4561-ac7f-899cc907d62a`). Only Action Groups can call the webhook. |
| `does not have the required app role` | The `ActionGroupsSecureWebhook` role is not assigned to Azure Monitor. Run `azd provision` again; the hook re-creates the assignment. Tokens can be cached for up to an hour after a fix. |
| `audience is not authorized` | See the next section. |

### Wrong audience

The app accepts `api://<client-id>` and `<client-id>`. If someone deleted or
recreated the app registration, the azd environment still holds the old IDs.
Compare:

```sh
azd env get-value SERVICE_HEALTH_API_CLIENT_ID
az containerapp show --name "$APP_NAME" --resource-group "$RG" \
  --query "properties.template.containers[0].env[?name=='SERVICE_HEALTH_EXPECTED_AUDIENCE'].value" -o tsv
```

To start over with a fresh app registration, clear the stored IDs and
provision again:

```sh
azd env set SERVICE_HEALTH_API_OBJECT_ID ""
azd env set SERVICE_HEALTH_API_CLIENT_ID ""
azd provision
```

### Graph permission errors

If the pre-provision hook fails with `Authorization_RequestDenied` or
`Insufficient privileges`, your account cannot create app registrations or
grant app roles. Either get **Application Administrator** or **Cloud
Application Administrator**, or ask an admin to run only the hook once:

```sh
# Run by the Entra admin in their own clone of the repository
azd env new shh-prod --subscription "<subscription-id>" --location eastus2
azd hooks run preprovision
azd env get-values | grep -E "AZURE_TENANT_ID|SERVICE_HEALTH_API_"
```

The admin sends you the four values; you set them with `azd env set` and run
`azd provision`. With those IDs in place, the hook only reads from Graph.

If the hook says the Azure CLI is signed in to a different tenant than the
subscription, run `az login --tenant <tenant-id>` and try again.

### Key Vault name is soft-deleted

Key Vault has purge protection, so after `azd down` the vault stays
soft-deleted for 90 days and its name cannot be reused. Provisioning the
**same environment name in the same subscription** fails with a message that
the vault exists in a deleted state. Use a new environment name, for example
`azd env new shh-prod2`. See
[Key Vault soft-delete](https://learn.microsoft.com/azure/key-vault/general/soft-delete-overview).

### `/readyz` returns 503

The configuration is invalid. The logs show the reason, usually a routing
JSON error such as a missing `default_channel_id` or a non-integer
`priority`. Fix `routes.json`, run `azd env set SERVICE_HEALTH_ROUTES_JSON
...` again, then `azd provision`.

### The test succeeds but nothing appears in Slack

Check `AppTraces` for `service_health_result`. `duplicate` or `stale` means
the event was already processed. See also
[Action group test troubleshooting](https://learn.microsoft.com/azure/azure-monitor/alerts/test-action-group-errors).

## 8. Costs, removal and known limitations

### Estimated cost

Rough monthly figures for one environment at low alert volume. Prices vary by
region; check the [pricing calculator](https://azure.microsoft.com/pricing/calculator/).

| Resource | Estimate |
|---|---|
| Container Apps, 1 replica, 0.5 vCPU / 1 GiB, always on | ≈ US$10–35 after the [monthly free grant](https://learn.microsoft.com/azure/container-apps/billing). Idle replicas bill at a lower rate. |
| Container Registry, Basic | ≈ US$5 |
| Log Analytics and Application Insights | A few dollars at this volume (pay per GB, 30-day retention) |
| Storage, Key Vault, Action Group, Activity Log Alert | Cents |
| **Total** | **≈ US$20–45 per month** |

One replica always runs so Azure Monitor never waits for a cold start. You
can set `minReplicas` to 0 in `infra/modules/container-app.bicep` to save
money, at the cost of slower first responses.

### Remove everything

```sh
azd down --purge
```

`azd down` deletes the resource group. Some things are left behind and need
separate cleanup:

- **Key Vault** stays soft-deleted for 90 days because purge protection is
  on. It costs nothing, but it still holds the Slack token, so also do the
  Slack step below.
- **Entra app registration**:
  `az ad app delete --id <SERVICE_HEALTH_API_CLIENT_ID>` (get the ID with
  `azd env get-value SERVICE_HEALTH_API_CLIENT_ID` **before** running
  `azd down`).
- **Alert resource groups in other subscriptions**:
  `az group delete --subscription <id> --name rg-<env>-service-health-alerts`.
- **Slack**: on <https://api.slack.com/apps>, open the app and remove it from
  the workspace. This revokes the token.

### Known limitations

- **Same tenant only.** Monitored subscriptions must be in the Entra tenant
  that owns the app registration.
- **One alert per subscription.** Use section 6 or Azure Policy to scale.
- **Public endpoints.** The webhook must be reachable by Azure Monitor, so
  ingress is public and protected by Entra tokens. Key Vault, Storage and
  Container Registry also allow public network access, with RBAC-only data
  access and shared keys disabled on Storage. Add private endpoints and a
  VNet-integrated environment if your policies require it.
- **At-least-once delivery.** If the container crashes after Slack accepts
  the first message but before its timestamp is saved, a retry can post a
  second message. Removing that window needs a transactional outbox, which is
  out of scope.
- **Permanent Slack errors are not retried.** An event that fails with
  `not_in_channel` is not reposted; the next update creates the message.
- **Token rotation needs a provision.** After rotating the Slack token, run
  `azd env set SLACK_BOT_TOKEN ...` and `azd provision`.
- **Local development needs a real Storage account.** There is no Azurite
  mode.

## How it works

1. Each subscription has an Activity Log Alert with `category ==
   ServiceHealth`. It calls an Action Group with a
   [Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook)
   using the [Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema).
2. [Container Apps authentication](https://learn.microsoft.com/azure/container-apps/authentication)
   validates the Entra token. The app then checks that the caller is Azure
   Monitor, that the token carries the `ActionGroupsSecureWebhook` role, and
   that the audience is this API. If `APP_ENV` is missing, the app assumes
   production and refuses to start the webhook without an expected audience.
3. The incident key is the subscription ID plus a hash of the Service Health
   tracking ID. A Table Storage entity holds the Slack channel, the message
   timestamp, the last processed update and a 30-second lease. Every write
   uses an ETag, so two replicas cannot claim the same incident.
4. Identical retries and out-of-order updates return 200 without touching
   Slack. Transient Slack or Storage errors return 503, so the Action Group
   [retries](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#webhook).
   Invalid payloads and permanent Slack errors return 4xx.
5. The first event posts a message; later events edit it. If someone deleted
   the message, the service posts a new one.

| Route | Purpose |
|---|---|
| `POST /api/service-health` | Webhook, Entra token required |
| `GET /healthz` | Liveness |
| `GET /readyz` | Configuration is valid |

Runtime settings (set by Bicep in Azure):

| Variable | Required | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot token with `chat:write` |
| `AZURE_TABLE_ENDPOINT` | Yes | Table endpoint for incident state |
| `SERVICE_HEALTH_ROUTES_JSON` or `SERVICE_HEALTH_ROUTES_FILE` | Yes | Routing |
| `SERVICE_HEALTH_EXPECTED_AUDIENCE` | Production | Comma-separated audiences, `api://<id>,<id>` |
| `APP_ENV` | No | `production` (default), `development` or `test`. The last two turn off the identity check. |
| `AZURE_CLIENT_ID` | Azure | Client ID of the user-assigned managed identity |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | No | Sends telemetry to Application Insights |
| `SERVICE_HEALTH_MAX_PAYLOAD_BYTES` | No | Request size limit, default 262144 |
| `LOG_LEVEL` | No | Default `INFO` |

## Local development

Local runs post to real Slack channels and use the Storage account of a
deployed environment, so deploy first.

```sh
python -m venv .venv
source .venv/bin/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-test.txt
pytest -q
flake8 .
```

Give yourself access to the incident table:

bash:

```bash
az role assignment create \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Storage Table Data Contributor" \
  --scope "$(az storage account show --name "$(azd env get-value AZURE_STORAGE_ACCOUNT_NAME)" --query id -o tsv)"
```

PowerShell:

```powershell
$storageId = az storage account show --name (azd env get-value AZURE_STORAGE_ACCOUNT_NAME) --query id -o tsv
az role assignment create `
  --assignee (az ad signed-in-user show --query id -o tsv) `
  --role "Storage Table Data Contributor" `
  --scope $storageId
```

Copy `.env-example` to `.env`, set `SLACK_BOT_TOKEN` and set
`AZURE_TABLE_ENDPOINT` to the output of
`azd env get-value AZURE_TABLE_ENDPOINT`. Keep `APP_ENV=development`; it turns
off the Entra check, so never use it in Azure. Then run the app and send the
sample payload:

```sh
python app.py
```

bash:

```bash
curl -sS -X POST http://localhost:5000/api/service-health \
  -H "Content-Type: application/json" \
  --data @docs/sample-service-health-alert.json
# {"correlationId":"...","result":"created"}
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:5000/api/service-health `
  -ContentType application/json -InFile docs/sample-service-health-alert.json
```

To see an update, change `stage` to `Resolved`, `status` to `Resolved`, and
move `submissionTimestamp` forward, then send it again. The same Slack
message changes to Resolved.

If your network requires a package mirror, build the image with
`docker build --build-arg PIP_INDEX_URL=<mirror> -t azure-service-health-slack .`.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
