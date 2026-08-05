<#
.SYNOPSIS
  Converts the Azure Migration Cost Estimator deployment to PRIVATE (internal) access
  reachable only from inside a VNet, browsed via Azure Bastion + a Windows jump VM.

  Creates / configures (idempotent where possible):
    - VNet + subnets: snet-aca (/23, delegated), AzureBastionSubnet (/26), snet-jump (/27)
    - Recreates the Container Apps Environment as INTERNAL (VNet-injected) and redeploys
      the app with private ingress (via infra/main.bicep -internal)
    - Private DNS zone for the environment default domain + wildcard A records -> static IP
    - Azure Bastion (Basic) + a Windows jump VM (no public IP)

  NOTE: A Container Apps environment's internal/external mode is IMMUTABLE, so the existing
  public environment + app are deleted and recreated. This removes public access.

  Prereqs: az CLI (az login). Run after deploy/provision.ps1 has created the ACR + image.

.EXAMPLE
  ./deploy/make-private.ps1 -ResourceGroup rg-cost-estimator -Location eastus -AcrName acrcost10071b20
#>
[CmdletBinding()]
param(
  [string]$ResourceGroup = 'rg-cost-estimator',
  [string]$Location      = 'eastus',
  [string]$AppName       = 'cost-estimator',
  [Parameter(Mandatory)] [string]$AcrName,
  [string]$VNetName      = 'vnet-cost-estimator',
  [string]$JumpVmSize    = 'Standard_F2as_v7',   # pick an unrestricted size in your region
  [string]$AdminUser     = 'azureuser'
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$repoRoot = Split-Path -Parent $PSScriptRoot
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# Work around a corrupt user CLI extension dir by using a clean one (az bastion needs an extension).
$cleanExt = Join-Path $env:TEMP 'azext_clean'
New-Item -ItemType Directory -Force $cleanExt | Out-Null
$env:AZURE_EXTENSION_DIR = $cleanExt

# ---- Network -----------------------------------------------------------------
Say "VNet $VNetName + subnets"
az network vnet create -g $ResourceGroup -n $VNetName --address-prefixes 10.30.0.0/16 `
  --subnet-name snet-aca --subnet-prefixes 10.30.0.0/23 -o none
az network vnet subnet update -g $ResourceGroup --vnet-name $VNetName -n snet-aca `
  --delegations Microsoft.App/environments -o none
az network vnet subnet create -g $ResourceGroup --vnet-name $VNetName -n AzureBastionSubnet --address-prefixes 10.30.2.0/26 -o none 2>$null
az network vnet subnet create -g $ResourceGroup --vnet-name $VNetName -n snet-jump --address-prefixes 10.30.2.64/27 -o none 2>$null
$subnetId = az network vnet subnet show -g $ResourceGroup --vnet-name $VNetName -n snet-aca --query id -o tsv

# ---- Tear down public env/app (internal mode is immutable) --------------------
Say 'Removing public Container App + environment (if present)…'
az containerapp delete -n $AppName -g $ResourceGroup --yes -o none 2>$null
az containerapp env delete -n "cae-$AppName" -g $ResourceGroup --yes -o none 2>$null

# ---- Deploy internal env + private app --------------------------------------
Say 'Deploying INTERNAL environment + private app (Bicep)…'
$image = "$AcrName.azurecr.io/${AppName}:latest"
$out = az deployment group create -g $ResourceGroup `
  --template-file (Join-Path $repoRoot 'infra/main.bicep') `
  --parameters appName=$AppName acrName=$AcrName containerImage=$image location=$Location `
               internal=true infrastructureSubnetId=$subnetId `
  --query properties.outputs -o json | ConvertFrom-Json
$staticIp = $out.envStaticIp.value
$domain   = $out.envDefaultDomain.value
$appFqdn  = $out.fqdn.value
Say "Private IP: $staticIp  |  FQDN: $appFqdn"

# ---- Private DNS -------------------------------------------------------------
Say "Private DNS zone $domain -> $staticIp"
az network private-dns zone create -g $ResourceGroup -n $domain -o none 2>$null
az network private-dns link vnet create -g $ResourceGroup -z $domain -n link-vnet `
  --virtual-network $VNetName --registration-enabled false -o none 2>$null
foreach ($rn in @('*', '*.internal', "$AppName.internal")) {
  az network private-dns record-set a add-record -g $ResourceGroup -z $domain -n $rn -a $staticIp -o none 2>$null
}

# ---- Jump VM -----------------------------------------------------------------
Say "Windows jump VM (vm-jump, $JumpVmSize, no public IP)"
$pw = 'Az!' + (-join ((1..16) | ForEach-Object { [char](((65..90)+(97..122)+(48..57) | Get-Random)) })) + '9#'
az vm create -g $ResourceGroup -n vm-jump `
  --image MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest `
  --size $JumpVmSize --admin-username $AdminUser --admin-password $pw `
  --vnet-name $VNetName --subnet snet-jump --public-ip-address '""' --nsg-rule NONE -o none

# ---- Bastion -----------------------------------------------------------------
Say 'Azure Bastion (Basic) — this takes several minutes…'
az network public-ip create -g $ResourceGroup -n pip-bastion --sku Standard --allocation-method Static -o none
az network bastion create -g $ResourceGroup -n bastion-cost --public-ip-address pip-bastion `
  --vnet-name $VNetName --location $Location --sku Basic -o none

Say 'Done.'
Write-Host ""
Write-Host "Private app URL (open FROM the jump VM): $appFqdn" -ForegroundColor Green
Write-Host "Jump VM: vm-jump   user: $AdminUser   password: $pw" -ForegroundColor Yellow
Write-Host "Access: Azure Portal -> vm-jump -> Connect -> Bastion -> sign in -> open Edge to the URL." -ForegroundColor Green
