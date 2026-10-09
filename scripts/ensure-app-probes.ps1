# Reapply Bicep after the first deploy; do not patch a second source of truth.
$ErrorActionPreference = "Stop"

$appName = $env:SERVICE_APP_NAME
$resourceGroup = $env:AZURE_RESOURCE_GROUP
$environmentName = $env:AZURE_ENV_NAME
if (-not $appName -or -not $resourceGroup -or -not $environmentName -or -not $env:AZURE_SUBSCRIPTION_ID) {
    throw "AZURE_ENV_NAME, AZURE_SUBSCRIPTION_ID, SERVICE_APP_NAME and AZURE_RESOURCE_GROUP are required. Run 'azd provision' first."
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it and run 'az login'."
}

function Get-AppContainer {
    $json = az containerapp show --name $appName --resource-group $resourceGroup `
        --subscription $env:AZURE_SUBSCRIPTION_ID `
        --query "properties.template.containers[?name=='app'] | [0]" `
        --output json --only-show-errors
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw "Could not read Container App '$appName' in '$resourceGroup'."
    }
    $container = $json | ConvertFrom-Json
    if (-not $container -or -not $container.image) {
        throw "Container App '$appName' has no application container/image."
    }
    return $container
}

function Test-AppProbes($container) {
    foreach ($required in @(
        @{ Type = "Liveness"; Path = "/healthz" },
        @{ Type = "Readiness"; Path = "/readyz" }
    )) {
        $matches = @($container.probes | Where-Object {
            $_.type -eq $required.Type -and $_.httpGet.path -eq $required.Path -and
            $_.httpGet.port -eq 5000 -and $_.httpGet.scheme -eq "HTTP"
        })
        if ($matches.Count -ne 1) { return $false }
    }
    return $true
}

$container = Get-AppContainer
if ($container.image -like "mcr.microsoft.com/azuredocs/containerapps-helloworld*") {
    throw "Container App '$appName' is still on the placeholder image. Run 'azd deploy' again."
}

if (Test-AppProbes $container) {
    Write-Host "Container App '$appName' already has health probes. Nothing to do."
    exit 0
}

Write-Host "Container App '$appName' is missing the required application probes."
Write-Host "Re-applying the infrastructure so the real image and probes are in the template..."

# Pin the image that is actually running so the provision cannot fall back to the placeholder.
azd env set SERVICE_APP_IMAGE_NAME $container.image --environment $environmentName | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to set SERVICE_APP_IMAGE_NAME in the azd environment." }
# A child azd process inherits the hook's environment, not just the updated .env.
$env:SERVICE_APP_IMAGE_NAME = $container.image

azd provision --environment $environmentName --no-prompt
if ($LASTEXITCODE -ne 0) {
    throw "azd provision failed. Fix the error above and run 'azd provision' to enable the health probes."
}

$verified = Get-AppContainer
if ($verified.image -ne $container.image -or -not (Test-AppProbes $verified)) {
    throw "Provision completed but the application image/probes do not match. Inspect the Container App before accepting this deployment."
}
