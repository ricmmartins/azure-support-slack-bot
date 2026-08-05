# Azure Deployment Plan

> **Status:** Ready for Validation

Generated: 2026-08-04

## 1. Project Overview

**Goal:** Add a production-oriented Azure Service Health to Slack integration to the existing Azure Support Slack Bot without changing its support-ticket workflow.

**Path:** Modernize Existing

## 2. Requirements

| Attribute | Value |
|---|---|
| Classification | Production-oriented MVP |
| Scale | Small, single region |
| Budget | Cost-optimized |
| Subscription | Supplied by the `azd` environment |
| Location | Supplied by the `azd` environment |
| Network | Public HTTPS endpoints with Entra ID, RBAC, and configurable firewalls |

## 3. Components Detected

| Component | Type | Technology | Path |
|---|---|---|---|
| Slack bot and webhook API | API | Python 3.13, Slack Bolt, Flask | repository root |
| Azure support integration | Service | Azure management SDKs | `azure_support.py` |

## 4. Recipe Selection

**Selected:** Azure Developer CLI with Bicep.

The project is Azure-only, already containerized, and has no existing IaC. AZD provides environment management while Bicep keeps the Azure resources explicit and repeatable.

## 5. Architecture

| Component | Azure Service | SKU |
|---|---|---|
| Slack bot and Service Health webhook | Azure Container Apps | Consumption |
| Container images | Azure Container Registry | Basic |
| Incident state and idempotency | Azure Table Storage | StorageV2 Standard_LRS |
| Secrets | Azure Key Vault | Standard |
| Telemetry | Application Insights + Log Analytics | Consumption / PerGB2018 |
| Alert delivery | Azure Monitor Activity Log Alert + Secure Action Group | Global |

The Container App uses Managed Identity for Storage, Key Vault, and ACR. Azure Monitor calls `POST /api/service-health` through a Secure Webhook using Common Alert Schema. Container Apps Easy Auth validates Microsoft Entra tokens and limits the client to the official AzNS AAD Webhook application. The existing `/slack/events` path remains protected by Slack request signing.

Azure Table Storage uses normalized `subscriptionId` as `PartitionKey` and a stable hash of `trackingId` as `RowKey`. ETags, a short processing lease, and payload fingerprints provide concurrency control, duplicate suppression, and stale-update protection.

## 6. Resource Inventory

No deployment is part of this change. Subscription usage and regional quotas must be checked with `azure-quotas` before a future `azd up`.

| Resource Type | Number to Deploy | Deployment Limit Strategy |
|---|---:|---|
| Microsoft.App/managedEnvironments | 1 | Validate before deployment |
| Microsoft.App/containerApps | 1 | Validate before deployment |
| Microsoft.ContainerRegistry/registries | 1 | Validate before deployment |
| Microsoft.Storage/storageAccounts | 1 | Validate before deployment |
| Microsoft.KeyVault/vaults | 1 | Validate before deployment |
| Microsoft.OperationalInsights/workspaces | 1 | Validate before deployment |
| Microsoft.Insights/components | 1 | Validate before deployment |
| Microsoft.Insights/actionGroups | 1 | Global resource |
| Microsoft.Insights/activityLogAlerts | 1 | Global resource |

## 7. Validation Proof

| Check | Command | Result |
|---|---|---|
| Python tests | `python -m pytest -q` | Passed: 45 tests |
| Python lint | `python -m flake8 .` | Passed |
| Bicep compilation | `az bicep build --file .\infra\main.bicep --stdout` | Passed without diagnostics |
| Bicep lint | `az bicep lint --file .\infra\main.bicep` | Passed without diagnostics |
| Entra setup syntax | PowerShell parser for `scripts\configure-secure-webhook.ps1` | Passed |
| AZD installed | `azd version` | Passed: 1.24.1 |
| AZD authentication | `azd auth login --check-status` | Passed |
| AZD package configuration | `azd package --no-prompt` | Passed; tagged the deployment image |
| Container package/build | `docker build --quiet -t azure-support-slack-bot:mvp .` | Passed |
| Container smoke test | Run final image and call `GET /healthz` | Passed as non-root user `app` |
| Provision preview | Not run | Requires an explicit target subscription/location and executes the Entra pre-provision hook |
| Azure Policy validation | Not run | Requires the future deployment subscription |

The plan cannot be marked `Validated` until the subscription-scoped preview and
policy checks pass. No resources were provisioned and no Microsoft Graph
changes were made during this validation.

### Required AZD validation checks

- [x] AZD installation
- [x] Azure YAML schema/package validation
- [ ] Environment setup with confirmed subscription and location
- [x] Authentication check
- [ ] Subscription/location check
- [x] Aspire checks skipped because this is not an Aspire project
- [ ] Provision preview
- [x] Container build verification
- [x] Package validation
- [ ] Azure Policy validation

## 8. Execution Checklist

### Preparation

- Analyze the existing Slack and Azure support flows.
- Implement Common Alert Schema parsing, routing, Table state, Slack lifecycle updates, security, retries, and observability.
- Generate AZD/Bicep, Entra configuration script, runtime configuration, and documentation.
- Add unit, integration, and regression tests.
- Update this status to `Ready for Validation`.

### Validation

- Run the Python test suite and lint.
- Build and start the container; verify health probes.
- Validate Bicep and AZD configuration.
- Invoke `azure-validate`.
- Do not deploy without an explicit follow-up request.

## 9. Files Generated

| File | Purpose |
|---|---|
| `azure.yaml` | AZD service definition |
| `infra/main.bicep` | Subscription deployment entry point |
| `infra/main.parameters.json` | AZD parameters |
| `infra/modules/*.bicep` | Resource-group-scoped Azure resources |
| `scripts/configure-secure-webhook.ps1` | Idempotent Microsoft Graph setup |
| `service_health/*.py` | Webhook domain and integrations |
