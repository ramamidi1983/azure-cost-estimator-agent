<#
.SYNOPSIS
  Enable Microsoft Entra ID authentication (Easy Auth) on the Container App so it
  requires sign-in over the public internet. No app code changes required.

.DESCRIPTION
  - Creates a single-tenant Entra app registration with the ACA Easy Auth redirect URI.
  - Adds a client secret and wires it into the Container App's built-in auth.
  - Sets unauthenticated browser requests to redirect to the Microsoft sign-in page.

.EXAMPLE
  ./deploy/enable-entra-auth.ps1 -ResourceGroup rg-cost-estimator -AppName cost-estimator-pub
#>
param(
  [Parameter(Mandatory)][string]$ResourceGroup,
  [Parameter(Mandatory)][string]$AppName,
  [string]$DisplayName = "cost-estimator-auth",
  [int]$SecretYears = 2
)
$ErrorActionPreference = "Stop"

$fqdn = az containerapp show -g $ResourceGroup -n $AppName --query "properties.configuration.ingress.fqdn" -o tsv
if (-not $fqdn) { throw "Could not read ingress FQDN for $AppName. Is external ingress enabled?" }
$redirect = "https://$fqdn/.auth/login/aad/callback"
$tenant   = az account show --query tenantId -o tsv
$issuer   = "https://login.microsoftonline.com/$tenant/v2.0"

Write-Host "App FQDN : $fqdn"
Write-Host "Redirect : $redirect"
Write-Host "Tenant   : $tenant"

Write-Host "Creating Entra app registration..."
$appId = az ad app create --display-name $DisplayName `
  --web-redirect-uris $redirect `
  --enable-id-token-issuance true `
  --sign-in-audience AzureADMyOrg `
  --query appId -o tsv

Write-Host "Adding client secret..."
$secret = (az ad app credential reset --id $appId --display-name "easyauth" --years $SecretYears --query password -o tsv | Select-Object -Last 1).Trim()

Write-Host "Configuring Microsoft identity provider on the Container App..."
az containerapp auth microsoft update -g $ResourceGroup -n $AppName `
  --client-id $appId --client-secret $secret --issuer $issuer --yes -o none

Write-Host "Requiring authentication (redirect unauthenticated browsers to sign-in)..."
az containerapp auth update -g $ResourceGroup -n $AppName `
  --unauthenticated-client-action RedirectToLoginPage `
  --redirect-provider azureactivedirectory `
  --enabled true --require-https true -o none

Write-Host "Restarting active revision to apply the secret..."
$rev = az containerapp show -g $ResourceGroup -n $AppName --query "properties.latestReadyRevisionName" -o tsv
az containerapp revision restart -g $ResourceGroup -n $AppName --revision $rev -o none

Write-Host ""
Write-Host "Entra ID auth enabled. App (requires sign-in): https://$fqdn"
Write-Host "App registration client-id: $appId"
