// Azure Migration Cost Estimator - Container Apps hosting.
// Provisions: Log Analytics, Container Apps Environment, user-assigned identity
// (AcrPull on an existing ACR), and the Container App running the Streamlit dashboard.
@description('Location for all resources.')
param location string = resourceGroup().location

@description('Base name used to derive resource names.')
param appName string = 'cost-estimator'

@description('Name of an EXISTING Azure Container Registry (created by provision script).')
param acrName string

@description('Full container image reference, e.g. myacr.azurecr.io/cost-estimator:latest')
param containerImage string

@description('Container listening port.')
param targetPort int = 8501

@description('Min / max replicas.')
param minReplicas int = 0
param maxReplicas int = 3

@description('Make ingress internal (private VNet IP only).')
param internal bool = false

@description('Resource ID of the infrastructure subnet (required when internal=true).')
param infrastructureSubnetId string = ''

var logName = 'log-${appName}'
var envName = 'cae-${appName}'
var uamiName = 'id-${appName}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    vnetConfiguration: internal ? {
      internal: true
      infrastructureSubnetId: infrastructureSubnetId
    } : null
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: !internal
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'STREAMLIT_SERVER_PORT', value: string(targetPort) }
            { name: 'STREAMLIT_SERVER_ADDRESS', value: '0.0.0.0' }
            { name: 'STREAMLIT_SERVER_HEADLESS', value: 'true' }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scale'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
  dependsOn: [ acrPull ]
}

output fqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appName string = app.name
output acrLoginServer string = acr.properties.loginServer
output envStaticIp string = env.properties.staticIp
output envDefaultDomain string = env.properties.defaultDomain
