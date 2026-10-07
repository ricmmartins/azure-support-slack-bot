// Onboards one more subscription to an existing deployment.
// Creates a resource group in the target subscription with its own Action
// Group and Service Health alert, both pointing at the shared webhook.
//
// az account set --subscription <target-subscription-id>
// az deployment sub create --location <region> \
//   --template-file infra/alert-subscription.bicep \
//   --parameters environmentName=<azd-env-name> \
//     webhookUri=<SERVICE_HEALTH_WEBHOOK_URI> \
//     secureWebhookObjectId=<SERVICE_HEALTH_API_OBJECT_ID> \
//     secureWebhookIdentifierUri=<SERVICE_HEALTH_API_IDENTIFIER_URI>
targetScope = 'subscription'

@minLength(1)
@description('azd environment name of the main deployment; used for resource names.')
param environmentName string

@description('Region of the resource group that holds the alert resources.')
param location string = deployment().location

@description('Webhook URL of the main deployment (azd output SERVICE_HEALTH_WEBHOOK_URI).')
param webhookUri string

@description('Object ID of the Entra app registration that protects the webhook (SERVICE_HEALTH_API_OBJECT_ID).')
param secureWebhookObjectId string

@description('Identifier URI of that app registration, for example api://<client-id> (SERVICE_HEALTH_API_IDENTIFIER_URI).')
param secureWebhookIdentifierUri string

@description('Entra tenant ID. The target subscription must belong to the same tenant as the app registration.')
param tenantId string = tenant().tenantId

param resourceGroupName string = 'rg-${environmentName}-service-health-alerts'

var tags = {
  'azd-env-name': environmentName
  workload: 'azure-service-health-slack'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module serviceHealthAlert 'modules/service-health-alert.bicep' = {
  scope: resourceGroup
  name: 'service-health-alert'
  params: {
    environmentName: environmentName
    webhookUri: webhookUri
    secureWebhookObjectId: secureWebhookObjectId
    secureWebhookIdentifierUri: secureWebhookIdentifierUri
    tenantId: tenantId
    targetSubscriptionId: subscription().subscriptionId
    tags: tags
  }
}

output actionGroupId string = serviceHealthAlert.outputs.actionGroupId
output activityLogAlertId string = serviceHealthAlert.outputs.activityLogAlertId
