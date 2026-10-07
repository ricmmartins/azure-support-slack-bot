# Azure Deployment Plan

> **Status:** Ready for Validation

Generated: 2026-08-04

## 1. Project Overview

**Goal:** Deliver a production-oriented, notification-only Azure Service Health to Slack service. The support-ticket code inherited from Azure-Samples/azure-support-slack-bot was removed.

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
| Service Health webhook API | API | Python 3.13, Flask, Slack SDK | repository root, `service_health/` |

## 4. Recipe Selection

**Selected:** Azure Developer CLI with Bicep.

The project is Azure-only, already containerized, and has no existing IaC. AZD provides environment management while Bicep keeps the Azure resources explicit and repeatable.

## 5. Architecture

| Component | Azure Service | SKU |
|---|---|---|
| Service Health webhook | Azure Container Apps | Consumption |
| Container images | Azure Container Registry | Basic |
| Incident state and idempotency | Azure Table Storage | StorageV2 Standard_LRS |
| Secrets | Azure Key Vault | Standard |
| Telemetry | Application Insights + Log Analytics | Consumption / PerGB2018 |
| Alert delivery | Azure Monitor Activity Log Alert + Secure Action Group | Global |

The Container App uses Managed Identity for Storage, Key Vault, and ACR. Azure Monitor calls `POST /api/service-health` through a Secure Webhook using Common Alert Schema. Container Apps Easy Auth validates Microsoft Entra tokens and limits the client to the official AzNS AAD Webhook application. Slack is outbound only (`chat:write`); there is no inbound Slack endpoint.

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
| Python tests | `python -m pytest -q` | Passed: 42 tests (includes production-hardening regressions) |
| Python lint | `python -m flake8 .` | Passed |
| Dependency advisories | OSV.dev batch query for pinned and transitive packages | Fixed: Flask 3.1.3, python-dotenv 1.2.2, transitive security floors added |
| Static security patterns | grep for eval/exec/pickle/shell/TLS bypass/hardcoded secrets | No findings |
| Dependency audit (CI) | `pip-audit -r requirements.txt` in GitHub Actions | Passed: no known vulnerabilities |
| Bicep compilation | `az bicep build --file .\infra\main.bicep --stdout` | Passed without diagnostics |
| Bicep lint | `az bicep lint --file .\infra\main.bicep` | Passed without diagnostics |
| Entra setup syntax | PowerShell parser for `scripts\configure-secure-webhook.ps1` | Passed |
| AZD installed | `azd version` | Passed: 1.24.1 |
| AZD authentication | `azd auth login --check-status` | Passed |
| AZD package configuration | `azd package --no-prompt` | Passed before hardening; not re-run (Docker daemon unavailable) |
| Container package/build | `docker build` in GitHub Actions (`.github/workflows/ci.yml`) | Passed after hardening |
| Container smoke test | Run CI image and call `GET /healthz` | Passed as non-root user `app` |
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
- [x] Container build verification (CI)
- [x] Package validation
- [ ] Azure Policy validation

## 8. Execution Checklist

### Preparation

- Analyze the existing Slack integration.
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
