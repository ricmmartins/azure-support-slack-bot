# Is it Azure or is it us? Routing Azure Service Health into Slack for multi-subscription teams

*Audience: platform, SRE and on-call teams at growth- and late-stage startups
running production on Azure across several subscriptions.*

At 2:14 a.m. your p99 latency doubles. The on-call engineer opens the
dashboards, sees errors from the database client, and starts the usual
checklist: recent deploys, feature flags, connection pools. Forty minutes
later someone opens the Azure portal and finds a Service Health incident for
the database service in your region, published twenty minutes before the page
went out.

Those forty minutes are the gap this post is about. Azure publishes service
issues, planned maintenance, health advisories and security advisories through
[Azure Service Health](https://learn.microsoft.com/azure/service-health/overview),
scoped to the subscriptions, services and regions you actually use. The data
is there. What most teams lack is getting it to the right people, in the place
they already work, without adding noise.

We built a small open-source service that does that: it turns Service Health
events into Slack messages, routes them to the right channel, and keeps **one
message per incident** that is edited as the incident progresses. This post
covers why we built it the way we did, the trade-offs, and a full tutorial you
can follow end to end.

Code: <https://github.com/ricmmartins/azure-support-slack-bot>

## The problem with the defaults

Service Health alerts already support email, SMS and webhooks through
[Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups).
At startup scale, three things get in the way:

- **Noise.** A single incident produces several notifications: Active, a
  handful of updates, Resolved. Sent to email or posted as new chat messages,
  they bury each other. People mute the channel, and then they miss the next
  real one.
- **Ownership.** With a production subscription per product, plus shared
  platform and data subscriptions, a database incident in East US matters to
  one team and is noise to everyone else.
- **Context.** During an incident the first question is "is it Azure or is it
  us?" The answer should be in the channel where the incident is already being
  discussed, not in someone's inbox.

The goal was simple: when Azure has a problem that affects you, the right
channel sees one message within seconds, and that message tells the full story
as it changes.

## Architecture

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

Each monitored subscription has an Activity Log Alert on the `ServiceHealth`
category. It triggers an Action Group that calls the service with a
[Secure Webhook](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#secure-webhook)
using the
[Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema).
The service runs on Azure Container Apps, stores a small record per incident in
Table Storage, and posts or edits a Slack message. Slack is output only: the
bot has one scope, `chat:write`, and receives nothing from Slack.

## Design decisions and trade-offs

### Authentication: Entra tokens, not shared secrets

The usual way to protect a webhook is a secret in the URL or a header. Those
leak into logs and need rotation. Action Groups can instead obtain a Microsoft
Entra token for an app registration you own and send it as a bearer token.

The service validates it in two layers. First,
[Container Apps authentication](https://learn.microsoft.com/azure/container-apps/authentication)
(Easy Auth) verifies the signature, issuer and audience before the request
reaches the code. Then the app checks three claims itself: the caller is the
Azure Monitor application, the token carries the `ActionGroupsSecureWebhook`
app role, and the audience is this API. There is no shared secret to rotate,
and the endpoint can stay public because unauthenticated calls get a 401.

The cost is setup complexity: an app registration, an app role, and a role
assignment to the Azure Monitor service principal. A pre-provision script does
this, but it needs Entra permissions that many engineers do not have. We
document a path where an admin runs that one step and hands over four IDs.

### State: one message per incident

Service Health identifies an incident with a tracking ID, and every update
reuses it. The service keys a Table Storage entity on the subscription ID and
a hash of that tracking ID. The entity holds the Slack channel, the message
timestamp and the last processed update. The first event posts a message;
later events call `chat.update` on it.

Why Table Storage rather than Cosmos DB or a SQL database? The workload is a
few writes per incident, and we only need single-entity reads and conditional
writes. Table Storage does that for cents a month, with no capacity planning,
and supports Entra-only access with shared keys turned off.

### Concurrency and retries

Action Groups
[retry](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups#webhook)
on 408, 429, 503, 504 and network errors. Retries can overlap, and with more
than one replica two requests for the same incident can land at the same time.
Three rules keep that safe:

1. Every write to the incident entity uses an ETag. A short lease (30 seconds)
   lets exactly one request work on an incident at a time; the other gets a
   503 and is retried later.
2. Identical retries and updates older than the last processed one return 200
   and do nothing.
3. Status codes match the retry policy. Transient Slack or Storage problems
   return 503 so Azure Monitor retries. Invalid payloads and permanent Slack
   errors (for example, the bot is not in the channel) return 4xx, so a broken
   configuration does not cause a retry storm.

What we did **not** solve: if the container crashes after Slack accepts the
first post but before its timestamp is saved, a retry posts a second message.
Closing that window needs a transactional outbox. For a notification channel,
an occasional duplicate is acceptable; a missed incident is not.

### Routing across subscriptions

Routing is a JSON document with a default channel and rules that filter by
subscription, service and region. The most specific, highest-priority rule
wins, and an incident stays in the channel where it was first posted. That
maps to how most platform teams are organized: per-product channels for their
subscriptions, a data channel for database services, and a catch-all for the
platform team.

Onboarding another subscription is one `az deployment sub create` command
that adds an alert and an Action Group pointing at the same webhook. For
dozens of subscriptions, the same settings can be applied with
[Azure Policy](https://learn.microsoft.com/azure/service-health/service-health-alert-deploy-policy).

### Cost versus latency

The Container App runs one always-on replica with 0.5 vCPU and 1 GiB, so Azure
Monitor never waits on a cold start. With the
[Container Apps free grant](https://learn.microsoft.com/azure/container-apps/billing),
the whole stack costs roughly **US$20–45 per month**. Setting `minReplicas` to
0 cuts most of the compute cost if you can live with a slower first message.

### Secrets and identity

The Slack token is the only secret. azd writes it to Key Vault during
provisioning, and the container gets a Key Vault reference resolved through a
user-assigned managed identity. The same identity reads and writes Table
Storage and pulls the image from Container Registry. The app has no
connection strings or storage keys.

## Tutorial

You need about 30 minutes. Commands are for bash; PowerShell 7 versions follow
where they differ.

### 1. Prerequisites

Install:

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell),
  required on every OS because the setup hook is a PowerShell script
- [Python 3.11+](https://www.python.org/downloads/), optional, for local runs
  and tests
- [Docker](https://docs.docker.com/get-docker/), optional; the image is built
  in Azure Container Registry

Permissions:

- **Microsoft Entra ID:**
  [Application Administrator](https://learn.microsoft.com/entra/identity/role-based-access-control/permissions-reference#application-administrator)
  or Cloud Application Administrator, to create the app registration and grant
  its role to Azure Monitor.
- **Azure subscription:**
  [Owner](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles),
  or Contributor plus User Access Administrator, because the template creates
  role assignments.

```bash
git clone https://github.com/ricmmartins/azure-support-slack-bot.git
cd azure-support-slack-bot
```

### 2. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a
   manifest**, pick your workspace, choose YAML, and paste the contents of
   `slack_app_manifest.yaml` from the repository.
2. Select **Install to Workspace** → **Allow**.
3. Under **OAuth & Permissions**, copy the **Bot User OAuth Token**
   (`xoxb-...`).
4. In every channel the bot should post to, run:

   ```text
   /invite @azure-service-health
   ```

5. Get each channel ID: click the channel name, open **About**, and copy the
   **Channel ID** at the bottom (it looks like `C0123456789`).

### 3. Write the routing configuration

Create `routes.json`:

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

Every filter in a rule must match. Unmatched incidents go to
`default_channel_id`. Use the service and region names shown in Service
Health, such as `East US` rather than `eastus`.

### 4. Deploy

Sign in to both CLIs with the same account and tenant, then create an
environment:

```bash
az login
azd auth login
azd env new shh-prod --subscription "<subscription-id>" --location eastus2
```

Set the Slack token and the routing JSON (as a single line):

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

Provision, deploy, and provision again:

```bash
azd provision
azd deploy
azd provision
```

- The **first provision** runs the hook that creates the Entra app
  registration and app role and grants the role to Azure Monitor. It stores
  `AZURE_TENANT_ID`, `SERVICE_HEALTH_API_CLIENT_ID`,
  `SERVICE_HEALTH_API_OBJECT_ID` and `SERVICE_HEALTH_API_IDENTIFIER_URI` in the
  azd environment, then creates all Azure resources. The Container App starts
  with a placeholder image and no health probes, because your image does not
  exist yet.
- **`azd deploy`** builds the image in Container Registry and rolls it out.
- The **second provision** applies the real image to the template and turns
  on the `/healthz` and `/readyz` probes.

After that, use `azd deploy` for code changes and `azd provision` for
configuration changes.

If you lack the Entra role, an admin can run `azd hooks run preprovision` in
their own azd environment and send you the four `AZURE_TENANT_ID` /
`SERVICE_HEALTH_API_*` values to set with `azd env set`.

### 5. Validate

```bash
APP_URI=$(azd env get-value SERVICE_APP_URI)
RG=$(azd env get-value AZURE_RESOURCE_GROUP)
APP_NAME=$(azd env get-value SERVICE_APP_NAME)
ENV_NAME=$(azd env get-value AZURE_ENV_NAME)

curl -sS "$APP_URI/healthz"     # {"status":"healthy"}
curl -sS "$APP_URI/readyz"      # {"status":"ready"}

# Anonymous calls must be rejected
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$APP_URI/api/service-health" \
  -H "Content-Type: application/json" --data @docs/sample-service-health-alert.json
# 401
```

PowerShell:

```powershell
$APP_URI = azd env get-value SERVICE_APP_URI
Invoke-RestMethod "$APP_URI/healthz"
Invoke-RestMethod "$APP_URI/readyz"
```

Now send a signed test from Azure Monitor. In the portal, open **Monitor** →
**Alerts** → **Action groups** → `ag-<env>-service-health` → **Test**, choose
**Service health alert**, and run it. A message titled
`TestActionGroup-TestServiceHealthAlert` appears in your default channel. The
same test from the CLI:

```bash
az monitor action-group test-notifications create \
  --resource-group "$RG" \
  --action-group "ag-$ENV_NAME-service-health" \
  --alert-type servicehealth \
  --add-action webhook slack-service-health "$(azd env get-value SERVICE_HEALTH_WEBHOOK_URI)" \
    useaadauth "$(azd env get-value SERVICE_HEALTH_API_OBJECT_ID)" \
    "$(azd env get-value SERVICE_HEALTH_API_IDENTIFIER_URI)" usecommonalertschema
```

The [test payload](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-payload-samples#sample-test-action-service-health-alert)
always uses the same tracking ID, so a second test returns `duplicate` and
posts nothing. That is deduplication working.

Logs and telemetry:

```bash
az containerapp logs show --name "$APP_NAME" --resource-group "$RG" --follow --tail 50
```

In the Log Analytics workspace `log-<env>`:

```kusto
AppRequests
| where Url has "/api/service-health"
| summarize count() by ResultCode, bin(TimeGenerated, 1h)
```

### 6. Add more subscriptions

For each additional subscription in the same tenant:

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

PowerShell uses the same command with backticks for line breaks and
`(azd env get-value NAME)` in place of `"$(...)"`; the README has the full
version.

### 7. When something fails

| Symptom | Cause and fix |
|---|---|
| 422 and `not_in_channel` or `channel_not_found` in the logs | The bot is not in the channel, or the ID is wrong. Run `/invite @azure-service-health` and use the `C...` ID. |
| 401 `authentication_required` | The Action Group webhook does not have the secure webhook option on. Run `azd provision`. |
| 401 with no body | Easy Auth rejected the token, usually an audience mismatch. Check `az containerapp auth show`. |
| 403 and `does not have the required app role` | The role assignment to Azure Monitor is missing. Run `azd provision`; tokens can be cached for up to an hour. |
| 403 and `audience is not authorized` | The app registration was recreated. Clear `SERVICE_HEALTH_API_OBJECT_ID` and `SERVICE_HEALTH_API_CLIENT_ID` with `azd env set` and provision again. |
| Hook fails with `Authorization_RequestDenied` | Missing Entra role. Ask an admin to run `azd hooks run preprovision`. |
| Provision fails because the Key Vault exists in a deleted state | Purge protection keeps deleted vaults for 90 days. Use a new azd environment name. |
| 503 for a few minutes after the first deploy | Role assignments are still propagating. Azure Monitor retries on its own. |

### 8. Cost and cleanup

Expect roughly US$20–45 per month (Container Apps is most of it). Check your
region with the [pricing calculator](https://azure.microsoft.com/pricing/calculator/).

To remove everything:

```bash
CLIENT_ID=$(azd env get-value SERVICE_HEALTH_API_CLIENT_ID)
azd down --purge
az ad app delete --id "$CLIENT_ID"
```

Also delete `rg-<env>-service-health-alerts` in any extra subscriptions, and
remove the app from Slack to revoke the token. The Key Vault stays
soft-deleted for 90 days because purge protection is on.

## Known limitations

- Monitored subscriptions must be in the same Entra tenant.
- Ingress is public, protected by Entra tokens. Key Vault, Storage and
  Container Registry allow public network access with RBAC-only data access.
  Add private endpoints if your compliance baseline requires them.
- A crash at the wrong moment can cause a duplicate Slack message.
- Rotating the Slack token needs `azd env set` and `azd provision`.

## Where to take it next

Ideas that fit a late-stage platform team:

- Roll out the alert with Azure Policy so every new subscription is covered
  from day one.
- Add a rule per product team and link each message to your runbook or
  status page.
- Forward the same events to your incident tool (PagerDuty, Opsgenie,
  incident.io) for Sev1-level service issues in your primary regions.
- Track `service_health.requests` in Application Insights and alert when the
  service returns 503s for more than a few minutes.

If you are building on Azure, the
[Microsoft for Startups](https://www.microsoft.com/startups) program offers
Azure credits, technical guidance and access to Microsoft experts as you
scale. Issues and pull requests on the repository are welcome.
