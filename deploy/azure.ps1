# Deploys the web player to Azure Container Apps with a system-assigned managed identity
# that calls Microsoft Foundry keylessly (RBAC role "Foundry User" on the Foundry resource).
#
# Prerequisites: Azure CLI, `az login`, an existing Foundry resource + project + model deployment.
# Usage: ./deploy/azure.ps1 -FoundryResource <name> -FoundryProject <project> [-Tag v2]

param(
    [string]$ResourceGroup = "rg-insidethegame",
    [string]$Location = "swedencentral",
    [string]$Registry = "insidethegamekracr",
    [string]$Environment = "insidethegame-env",
    [string]$App = "insidethegame-player",
    [Parameter(Mandatory = $true)][string]$FoundryResource,
    [Parameter(Mandatory = $true)][string]$FoundryProject,
    [string]$Model = "gpt-5.4-mini",
    [string]$Tag = "v1"
)
$ErrorActionPreference = "Stop"

foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ContainerRegistry") {
    az provider register --namespace $ns --wait -o none
}
az extension add --name containerapp --upgrade --only-show-errors -o none

# 1. Registry + cloud build (no local Docker needed)
az acr create -g $ResourceGroup -n $Registry --sku Basic -l $Location -o none
az acr build -r $Registry -t "player:$Tag" --no-logs . -o none

# 2. Container Apps environment + app with system-assigned identity (scale to zero when idle)
$endpoint = "https://$FoundryResource.services.ai.azure.com/api/projects/$FoundryProject"
az containerapp env create -n $Environment -g $ResourceGroup -l $Location --only-show-errors -o none
$exists = az containerapp show -n $App -g $ResourceGroup --query name -o tsv 2>$null
if ($exists) {
    az containerapp update -n $App -g $ResourceGroup --image "$Registry.azurecr.io/player:$Tag" -o none
} else {
    az containerapp create -n $App -g $ResourceGroup --environment $Environment `
        --image "$Registry.azurecr.io/player:$Tag" --registry-server "$Registry.azurecr.io" --registry-identity system `
        --system-assigned --ingress external --target-port 8000 --min-replicas 0 --max-replicas 2 `
        --cpu 0.5 --memory 1.0Gi `
        --env-vars FOUNDRY_PROJECT_ENDPOINT=$endpoint FOUNDRY_MODEL=$Model FOUNDRY_AUTH=default `
                   LIVE_FOUNDRY_MAX=2 INSIGHTS_AGENT_TIMEOUT_S=45 -o none
}

# 3. Keyless access to Foundry for the app's managed identity
$principal = az containerapp show -n $App -g $ResourceGroup --query identity.principalId -o tsv
$scope = az cognitiveservices account show -g $ResourceGroup -n $FoundryResource --query id -o tsv
az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal `
    --role "Foundry User" --scope $scope -o none 2>$null

$fqdn = az containerapp show -n $App -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host "Player: https://$fqdn"
