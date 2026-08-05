<#
.SYNOPSIS
  Tear down the optional private-networking resources (Bastion, jump VM, internal
  Container Apps environment, VNet, private DNS) to stop their ongoing cost.

.DESCRIPTION
  Deletes only the private-hosting resources created by make-private.ps1. Leaves the
  public environment/app, ACR, and managed identity intact. Safe to run after moving
  to the public + Entra ID auth hosting model.

.EXAMPLE
  ./deploy/cleanup-private.ps1 -ResourceGroup rg-cost-estimator `
    -InternalEnv cae-cost-est2 -InternalApp cost-estimator `
    -Vnet vnet-cost-estimator -Bastion bastion-cost -BastionPip pip-bastion `
    -JumpVm vm-jump -PrivateDnsZone agreeabletree-1ba25373.eastus.azurecontainerapps.io
#>
param(
  [Parameter(Mandatory)][string]$ResourceGroup,
  [string]$InternalEnv,
  [string]$InternalApp,
  [string]$Vnet = "vnet-cost-estimator",
  [string]$Bastion = "bastion-cost",
  [string]$BastionPip = "pip-bastion",
  [string]$JumpVm = "vm-jump",
  [string]$PrivateDnsZone,
  [string]$VnetLinkName = "link-vnet"
)
$ErrorActionPreference = "Continue"

if ($InternalApp) { Write-Host "Deleting internal app $InternalApp...";  az containerapp delete -g $ResourceGroup -n $InternalApp --yes -o none }
if ($InternalEnv) { Write-Host "Deleting internal env $InternalEnv (no-wait)..."; az containerapp env delete -g $ResourceGroup -n $InternalEnv --yes --no-wait -o none }

Write-Host "Deleting Bastion $Bastion (slow)..."; az network bastion delete -g $ResourceGroup -n $Bastion -o none

Write-Host "Deleting VM $JumpVm + NIC/disk/NSG..."
az vm delete -g $ResourceGroup -n $JumpVm --yes -o none
az network nic delete -g $ResourceGroup -n "$($JumpVm)VMNic" -o none
$disk = az disk list -g $ResourceGroup --query "[?starts_with(name,'$($JumpVm)_OsDisk')].name" -o tsv
if ($disk) { az disk delete -g $ResourceGroup -n $disk --yes -o none }
az network nsg delete -g $ResourceGroup -n "$($JumpVm)NSG" -o none

Write-Host "Deleting Bastion public IP $BastionPip..."; az network public-ip delete -g $ResourceGroup -n $BastionPip -o none

if ($PrivateDnsZone) {
  Write-Host "Deleting private DNS zone link + zone..."
  az network private-dns link vnet delete -g $ResourceGroup -z $PrivateDnsZone -n $VnetLinkName --yes -o none
  az network private-dns zone delete -g $ResourceGroup -n $PrivateDnsZone --yes -o none
}

if ($InternalEnv) {
  Write-Host "Waiting for internal env deletion before removing the VNet..."
  for ($i=0; $i -lt 40; $i++) {
    if (-not (az containerapp env show -g $ResourceGroup -n $InternalEnv --query name -o tsv 2>$null)) { break }
    Start-Sleep 30
  }
}

Write-Host "Deleting VNet $Vnet..."; az network vnet delete -g $ResourceGroup -n $Vnet -o none
Write-Host "Cleanup complete. Remaining:"; az resource list -g $ResourceGroup --query "[].name" -o tsv
