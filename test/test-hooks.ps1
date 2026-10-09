# All CLI calls are mocked; this test never contacts Azure or Microsoft Graph.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot
$env:AZURE_ENV_NAME = "mock-env"
$env:AZURE_SUBSCRIPTION_ID = "mock-subscription"
$env:SERVICE_APP_NAME = "mock-app"
$env:AZURE_RESOURCE_GROUP = "mock-rg"
$requiredProbes = @(
    @{ type = "Liveness"; httpGet = @{ path = "/healthz"; port = 5000; scheme = "HTTP" } },
    @{ type = "Readiness"; httpGet = @{ path = "/readyz"; port = 5000; scheme = "HTTP" } }
)

function Assert($condition, $message) {
    if (-not $condition) { throw $message }
}

function global:az {
    $global:LASTEXITCODE = 0
    $global:reads++
    Assert ($args -contains "mock-subscription") "Subscription was not pinned"
    if ($global:scenario -eq "read-failure") {
        $global:LASTEXITCODE = 1
        return ""
    }
    $probes = $requiredProbes
    if ($global:reads -eq 1 -or $global:scenario -eq "verification-failure") {
        switch ($global:scenario) {
            "missing" { $probes = @() }
            "partial" { $probes = @($requiredProbes[0]) }
            "wrong-path" {
                $probes = @(@{ type = "Readiness"; httpGet = @{ path = "/"; port = 5000 } })
            }
            "verification-failure" { $probes = @() }
        }
    }
    $image = "mock.azurecr.io/app:v1"
    if ($global:scenario -eq "placeholder") {
        $image = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
    }
    return @{ image = $image; probes = $probes } | ConvertTo-Json -Depth 6
}

function global:azd {
    $global:LASTEXITCODE = 0
    Assert ($args -contains "mock-env") "Environment was not pinned"
    if ($args[0] -eq "provision") {
        $global:provisions++
        Assert ($env:SERVICE_APP_IMAGE_NAME -eq "mock.azurecr.io/app:v1") "Child inherited stale image"
        if ($global:scenario -eq "provision-failure") { $global:LASTEXITCODE = 1 }
    } elseif ($args[0] -eq "env") {
        $global:sets++
        if ($global:scenario -eq "set-failure") { $global:LASTEXITCODE = 1 }
    } else { throw "Unexpected azd command: $args" }
}

foreach ($scenario in @(
    "complete", "missing", "partial", "wrong-path", "placeholder",
    "read-failure", "set-failure", "provision-failure", "verification-failure"
)) {
    $global:scenario = $scenario
    $global:reads = 0
    $global:sets = 0
    $global:provisions = 0
    $env:SERVICE_APP_IMAGE_NAME = "stale-image"
    # Failure cases must reach the operation under test, not take the no-op.
    if ($scenario -in @("set-failure", "provision-failure")) {
        $savedProbes = $requiredProbes
        $requiredProbes = @()
    }
    $caught = $false
    try { & (Join-Path $root "scripts\ensure-app-probes.ps1") }
    catch { $caught = $true }
    if ($scenario -in @("set-failure", "provision-failure")) { $requiredProbes = $savedProbes }
    $shouldFail = $scenario -in @(
        "placeholder", "read-failure", "set-failure", "provision-failure", "verification-failure")
    Assert ($caught -eq $shouldFail) "Unexpected result for $scenario"
    if ($scenario -eq "complete") {
        Assert ($global:sets -eq 0 -and $global:provisions -eq 0) "Later deploy was not a no-op"
    }
    if ($scenario -in @("missing", "partial", "wrong-path")) {
        Assert ($global:sets -eq 1 -and $global:provisions -eq 1 -and $global:reads -eq 2) "Missing probes were not repaired/verified"
    }
}
Write-Host "Postdeploy: 9 mocked scenarios passed."

# Graph fixtures: recorded app failures must stop, disabled roles must stop,
# and assignments on later pages must not cause duplicate writes.
$env:SERVICE_HEALTH_API_OBJECT_ID = "api-object"
$env:SERVICE_HEALTH_API_CLIENT_ID = "api-client"
function global:az {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq "account") { return '{"tenantId":"mock-tenant"}' }
    Assert ($args[0] -eq "rest") "Unexpected az command"
    $uri = $args[[array]::IndexOf($args, "--uri") + 1]
    $method = $args[[array]::IndexOf($args, "--method") + 1]
    Assert ($method -eq "GET") "Unexpected Graph write"
    switch -Wildcard ($uri) {
        "*/applications/api-object" {
            if ($global:scenario -eq "graph-failure") {
                $global:LASTEXITCODE = 1
                return "Authorization_RequestDenied"
            }
            return @{
                id = "api-object"; appId = "api-client"
                api = @{ requestedAccessTokenVersion = 2 }
                identifierUris = @("api://api-client")
                appRoles = @(@{
                    id = "role-id"; value = "ActionGroupsSecureWebhook"
                    isEnabled = ($global:scenario -ne "disabled-role")
                    allowedMemberTypes = @("Application")
                })
            } | ConvertTo-Json -Depth 6
        }
        "*appId eq 'api-client'*" { return '{"value":[{"id":"api-principal"}]}' }
        "*appId eq '461e8683-5575-4561-ac7f-899cc907d62a'*" { return '{"value":[{"id":"monitor-principal"}]}' }
        "*/monitor-principal/appRoleAssignments" {
            return '{"value":[],"@odata.nextLink":"https://graph.microsoft.com/v1.0/next-page"}'
        }
        "*/next-page" {
            $global:nextPageRead = $true
            return '{"value":[{"resourceId":"api-principal","appRoleId":"role-id"}]}'
        }
        default { throw "Unexpected Graph URL: $uri" }
    }
}
function global:azd { $global:LASTEXITCODE = 0 }
foreach ($scenario in @("graph-failure", "disabled-role", "paginated-assignment")) {
    $global:scenario = $scenario
    $global:nextPageRead = $false
    $caught = $false
    try { & (Join-Path $root "scripts\configure-secure-webhook.ps1") }
    catch { $caught = $true }
    Assert ($caught -eq ($scenario -ne "paginated-assignment")) "Unexpected result for $scenario"
    if ($scenario -eq "paginated-assignment") {
        Assert $global:nextPageRead "Assignment pagination was not followed"
    }
}
Write-Host "Preprovision: 3 mocked scenarios passed. No Azure/Graph calls executed."
