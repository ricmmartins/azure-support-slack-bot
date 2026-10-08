# azd postdeploy hook.
# The first provision runs the Container App on a placeholder image without
# health probes, because the application image does not exist yet. After the
# first deploy, re-apply the infrastructure once so the template gets the real
# image and the /healthz and /readyz probes. Later deploys are a no-op here.
$ErrorActionPreference = "Stop"

$appName = $env:SERVICE_APP_NAME
$resourceGroup = $env:AZURE_RESOURCE_GROUP
if (-not $appName -or -not $resourceGroup) {
    throw "SERVICE_APP_NAME and AZURE_RESOURCE_GROUP are missing from the azd environment. Run 'azd provision' first."
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it and run 'az login'."
}

$subscriptionArgs = @()
if ($env:AZURE_SUBSCRIPTION_ID) { $subscriptionArgs = @("--subscription", $env:AZURE_SUBSCRIPTION_ID) }

$json = az containerapp show --name $appName --resource-group $resourceGroup @subscriptionArgs `
    --query "properties.template.containers[0].{image: image, probes: length(probes || ``[]``)}" `
    --output json --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $json) {
    throw "Could not read Container App '$appName' in '$resourceGroup'."
}
$container = $json | ConvertFrom-Json

if ([int]$container.probes -gt 0) {
    Write-Host "Container App '$appName' already has health probes. Nothing to do."
    exit 0
}

if (-not $container.image -or $container.image -like "mcr.microsoft.com/azuredocs/containerapps-helloworld*") {
    throw "Container App '$appName' is still on the placeholder image. Run 'azd deploy' again."
}

Write-Host "Container App '$appName' has no health probes yet (first deployment)."
Write-Host "Re-applying the infrastructure so the real image and probes are in the template..."

# Pin the image that is actually running so the provision cannot fall back to the placeholder.
azd env set SERVICE_APP_IMAGE_NAME $container.image | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to set SERVICE_APP_IMAGE_NAME in the azd environment." }

azd provision --no-prompt
if ($LASTEXITCODE -ne 0) {
    throw "azd provision failed. Fix the error above and run 'azd provision' to enable the health probes."
}
