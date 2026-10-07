[CmdletBinding()]
param(
    [string] $DisplayName = "Azure Support Slack Bot - $env:AZURE_ENV_NAME",
    [string] $AznsApplicationId = "461e8683-5575-4561-ac7f-899cc907d62a",
    [string] $RoleName = "ActionGroupsSecureWebhook"
)

$ErrorActionPreference = "Stop"

function Invoke-Graph {
    param(
        [Parameter(Mandatory)] [string] $Method,
        [Parameter(Mandatory)] [string] $Uri,
        [string] $Body,
        [int] $MaxAttempts = 6
    )
    # Graph is eventually consistent: newly created apps and service principals
    # can return 404/400 for a short time, so retry with backoff.
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $arguments = @("rest", "--method", $Method, "--uri", $Uri, "--only-show-errors")
        if ($Body) {
            $bodyFile = New-TemporaryFile
            Set-Content -Path $bodyFile -Value $Body -Encoding utf8
            $arguments += @("--headers", "Content-Type=application/json", "--body", "@$bodyFile")
        }
        try {
            $output = & az @arguments 2>&1
            $exitCode = $LASTEXITCODE
        } finally {
            if ($Body) { Remove-Item $bodyFile -ErrorAction SilentlyContinue }
        }
        if ($exitCode -eq 0) {
            $text = ($output | Where-Object { $_ -is [string] }) -join "`n"
            if ([string]::IsNullOrWhiteSpace($text)) { return $null }
            return $text | ConvertFrom-Json
        }
        if ($attempt -eq $MaxAttempts) {
            throw "Microsoft Graph $Method $Uri failed: $($output -join ' ')"
        }
        Start-Sleep -Seconds ([math]::Min(5 * $attempt, 20))
    }
}

function Set-AzdValue {
    param([string] $Name, [string] $Value)
    azd env set $Name $Value | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to set azd environment value $Name." }
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Run 'azd auth login' and install Azure CLI."
}

$account = az account show --output json --only-show-errors | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $account) {
    throw "No active Azure CLI session. Run 'az login'."
}

$tenantId = $account.tenantId
$application = $null
if ($env:SERVICE_HEALTH_API_OBJECT_ID) {
    # Prefer the app recorded in the azd environment over a display-name match.
    try {
        $application = Invoke-Graph -Method GET -MaxAttempts 1 `
            -Uri "https://graph.microsoft.com/v1.0/applications/$($env:SERVICE_HEALTH_API_OBJECT_ID)"
    } catch {
        $application = $null
    }
}
if (-not $application) {
    $escapedName = $DisplayName.Replace("'", "''")
    $apps = Invoke-Graph -Method GET `
        -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=displayName eq '$escapedName'"
    if (@($apps.value).Count -gt 1) {
        throw "More than one application is named '$DisplayName'. Set SERVICE_HEALTH_API_OBJECT_ID with 'azd env set' to choose one."
    }
    $application = $apps.value | Select-Object -First 1
}

if (-not $application) {
    $body = @{
        displayName = $DisplayName
        api = @{
            requestedAccessTokenVersion = 2
        }
    } | ConvertTo-Json -Depth 5
    $application = Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/applications" -Body $body
}

if ($application.api.requestedAccessTokenVersion -ne 2) {
    $patch = @{
        api = @{
            requestedAccessTokenVersion = 2
        }
    } | ConvertTo-Json -Depth 5
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Body $patch | Out-Null
}

$identifierUri = "api://$($application.appId)"
$role = $application.appRoles |
    Where-Object { $_.value -eq $RoleName } |
    Select-Object -First 1

if (-not $role) {
    $role = @{
        id = [guid]::NewGuid().ToString()
        allowedMemberTypes = @("Application")
        description = "Allows Azure Monitor Action Groups to invoke the secure webhook."
        displayName = $RoleName
        isEnabled = $true
        value = $RoleName
    }
    $appRoles = @($application.appRoles) + $role
    $patch = @{
        identifierUris = @($identifierUri)
        appRoles = $appRoles
    } | ConvertTo-Json -Depth 10
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Body $patch | Out-Null
} elseif ($application.identifierUris -notcontains $identifierUri) {
    $patch = @{ identifierUris = @($identifierUri) } | ConvertTo-Json
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Body $patch | Out-Null
}

$servicePrincipals = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$($application.appId)'"
$apiServicePrincipal = $servicePrincipals.value | Select-Object -First 1
if (-not $apiServicePrincipal) {
    $body = @{ appId = $application.appId } | ConvertTo-Json
    $apiServicePrincipal = Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" -Body $body
}

$aznsPrincipals = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$AznsApplicationId'"
$aznsPrincipal = $aznsPrincipals.value | Select-Object -First 1
if (-not $aznsPrincipal) {
    $body = @{ appId = $AznsApplicationId } | ConvertTo-Json
    $aznsPrincipal = Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" -Body $body
}

$assignments = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments"
$assignment = $assignments.value | Where-Object {
    $_.resourceId -eq $apiServicePrincipal.id -and $_.appRoleId -eq $role.id
}
if (-not $assignment) {
    $body = @{
        principalId = $aznsPrincipal.id
        resourceId = $apiServicePrincipal.id
        appRoleId = $role.id
    } | ConvertTo-Json
    Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($aznsPrincipal.id)/appRoleAssignments" -Body $body | Out-Null
}

Set-AzdValue AZURE_TENANT_ID $tenantId
Set-AzdValue SERVICE_HEALTH_API_CLIENT_ID $application.appId
Set-AzdValue SERVICE_HEALTH_API_OBJECT_ID $application.id
Set-AzdValue SERVICE_HEALTH_API_IDENTIFIER_URI $identifierUri

Write-Host "Secure webhook application is configured for $DisplayName."
