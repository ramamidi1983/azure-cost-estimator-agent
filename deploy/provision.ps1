<#
.SYNOPSIS
  End-to-end provisioning for the Azure Migration Cost Estimator dashboard.

  Creates (idempotently):
    - Resource group
    - Azure Container Registry (ACR)
    - Builds & pushes the container image (az acr build - no local Docker needed)
    - Log Analytics + Container Apps Environment + Container App (via infra/main.bicep)
    - A GitHub OIDC app registration + federated credential + role assignments
    - Sets the GitHub Actions repo variables used by .github/workflows/deploy.yml

  Prereqs: az CLI (logged in: az login), gh CLI (logged in: gh auth login).

.EXAMPLE
  ./deploy/provision.ps1 -ResourceGroup rg-cost-estimator -Location eastus -GitHubRepo ramamidi1983/azure-cost-estimator-agent
#>
[CmdletBinding()]
param(
  [string]$ResourceGroup = 'rg-cost-estimator',
  [string]$Location      = 'eastus',
  [string]$AppName       = 'cost-estimator',
  [string]$GitHubRepo    = 'ramamidi1983/azure-cost-estimator-agent',
  [string]$AcrName       = '',   # auto-generated if empty
  [switch]$SkipGitHubOidc
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# ---- Context -----------------------------------------------------------------
$sub    = az account show --query id -o tsv
$tenant = az account show --query tenantId -o tsv
if (-not $sub) { throw 'Not logged in. Run: az login' }
Say "Subscription: $sub"

if (-not $AcrName) { $AcrName = ('acrcost' + ($sub -replace '[^0-9a-f]','').Substring(0,8)) }
$image = "$AcrName.azurecr.io/${AppName}:latest"

# ---- Resource group ----------------------------------------------------------
Say "Resource group '$ResourceGroup' in $Location"
az group create -n $ResourceGroup -l $Location -o none

# ---- ACR + image build -------------------------------------------------------
Say "Container Registry '$AcrName'"
az acr create -g $ResourceGroup -n $AcrName --sku Basic --admin-enabled false -o none 2>$null
Say "Building & pushing image (az acr build)…"
az acr build -r $AcrName -t "${AppName}:latest" $repoRoot -o none

# ---- Infra (Bicep) -----------------------------------------------------------
Say 'Deploying Container Apps infrastructure (Bicep)…'
$outputs = az deployment group create -g $ResourceGroup `
  --template-file (Join-Path $repoRoot 'infra/main.bicep') `
  --parameters appName=$AppName acrName=$AcrName containerImage=$image location=$Location `
  --query properties.outputs -o json | ConvertFrom-Json

$appUrl = $outputs.appUrl.value
Say "App URL: $appUrl"

# ---- GitHub OIDC for CI/CD ---------------------------------------------------
if (-not $SkipGitHubOidc) {
  Say 'Configuring GitHub OIDC (federated identity, no stored secrets)…'
  $appDisplay = "gh-oidc-$AppName"
  $appId = az ad app list --display-name $appDisplay --query "[0].appId" -o tsv
  if (-not $appId) {
    $appId = az ad app create --display-name $appDisplay --query appId -o tsv
  }
  $spId = az ad sp list --filter "appId eq '$appId'" --query "[0].id" -o tsv
  if (-not $spId) { $spId = az ad sp create --id $appId --query id -o tsv }

  # Federated credential: main branch
  $fcName = 'gh-main'
  $exists = az ad app federated-credential list --id $appId --query "[?name=='$fcName'] | length(@)" -o tsv
  if ($exists -eq '0') {
    $fc = @{
      name      = $fcName
      issuer    = 'https://token.actions.githubusercontent.com'
      subject   = "repo:${GitHubRepo}:ref:refs/heads/main"
      audiences = @('api://AzureADTokenExchange')
    } | ConvertTo-Json -Compress
    $tmp = New-TemporaryFile
    $fc | Set-Content $tmp -Encoding utf8
    az ad app federated-credential create --id $appId --parameters $tmp -o none
    Remove-Item $tmp -Force
  }

  # Roles: Contributor + AcrPush on the resource group
  $spAppObjId = az ad sp show --id $appId --query id -o tsv
  az role assignment create --assignee-object-id $spAppObjId --assignee-principal-type ServicePrincipal `
    --role 'Contributor' --scope "/subscriptions/$sub/resourceGroups/$ResourceGroup" -o none 2>$null
  az role assignment create --assignee-object-id $spAppObjId --assignee-principal-type ServicePrincipal `
    --role 'AcrPush' --scope "/subscriptions/$sub/resourceGroups/$ResourceGroup" -o none 2>$null

  # Push config to GitHub Actions as repo variables
  Say 'Setting GitHub Actions repo variables…'
  gh variable set AZURE_CLIENT_ID       -R $GitHubRepo -b $appId
  gh variable set AZURE_TENANT_ID       -R $GitHubRepo -b $tenant
  gh variable set AZURE_SUBSCRIPTION_ID -R $GitHubRepo -b $sub
  gh variable set AZURE_RESOURCE_GROUP  -R $GitHubRepo -b $ResourceGroup
  gh variable set ACR_NAME              -R $GitHubRepo -b $AcrName
  gh variable set CONTAINERAPP_NAME     -R $GitHubRepo -b $AppName
}

Say 'Done.'
Write-Host ""
Write-Host "Dashboard:  $appUrl" -ForegroundColor Green
Write-Host "Push to 'main' now redeploys automatically via GitHub Actions." -ForegroundColor Green
