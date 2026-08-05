targetScope = 'subscription'

param environmentName string
param managedIdentityPrincipalId string

var readerRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'acdd72a7-3385-48ef-bd42-f606fba81ae7'
)
var supportRequestContributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'cfd33db0-3dd1-45e3-aa9d-cdbdf3b6f24e'
)

resource supportWorkflowReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    subscription().id,
    environmentName,
    managedIdentityPrincipalId,
    readerRole
  )
  properties: {
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: readerRole
  }
}

resource supportWorkflowContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    subscription().id,
    environmentName,
    managedIdentityPrincipalId,
    supportRequestContributorRole
  )
  properties: {
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: supportRequestContributorRole
  }
}
