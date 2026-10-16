# Event-driven ingest on top of deploy/azure.ps1:
#   data provider -> Azure Function (validate) -> Event Hubs -> insights worker (Container Apps)
#   -> Event Hubs broadcast -> web player (SSE to every viewer)
# Plus Application Insights for OpenTelemetry tracing of every agent run.
# All service-to-service auth uses managed identities + RBAC (no connection strings in code).

param(
    [string]$ResourceGroup = "rg-insidethegame",
    [string]$Location = "swedencentral",
    [string]$Registry = "insidethegamekracr",
    [string]$Environment = "insidethegame-env",
    [string]$Player = "insidethegame-player",
    [string]$Worker = "insidethegame-worker",
    [string]$EventHubs = "insidethegame-eh",
    [string]$Storage = "insidethegamest",
    [string]$Function = "insidethegame-ingest",
    [string]$AppInsights = "insidethegame-appi",
    [Parameter(Mandatory = $true)][string]$FoundryResource,
    [Parameter(Mandatory = $true)][string]$FoundryProject,
    [string]$Model = "gpt-5.4-mini",
    [string]$Tag = "v1"
)
$ErrorActionPreference = "Stop"
foreach ($ns in "Microsoft.EventHub", "Microsoft.Web", "Microsoft.Storage") { az provider register --namespace $ns --wait -o none }
az extension add --name application-insights --upgrade --only-show-errors -o none

# Observability
$law = az monitor log-analytics workspace list -g $ResourceGroup --query "[0].id" -o tsv
az monitor app-insights component create --app $AppInsights -g $ResourceGroup -l $Location --workspace $law --kind web -o none
$appi = az monitor app-insights component show --app $AppInsights -g $ResourceGroup --query connectionString -o tsv

# Event Hubs (partition key = match_id keeps each match ordered) + checkpoint storage
az eventhubs namespace create -g $ResourceGroup -n $EventHubs -l $Location --sku Basic -o none
foreach ($h in "match-events", "match-broadcast") {
    az eventhubs eventhub create -g $ResourceGroup --namespace-name $EventHubs -n $h --partition-count 2 --cleanup-policy Delete --retention-time-in-hours 24 -o none
}
az storage account create -g $ResourceGroup -n $Storage -l $Location --sku Standard_LRS --min-tls-version TLS1_2 --allow-blob-public-access false -o none
az storage container create --account-name $Storage -n checkpoints --auth-mode key -o none
$fqns = "$EventHubs.servicebus.windows.net"
$hubIn = az eventhubs eventhub show -g $ResourceGroup --namespace-name $EventHubs -n match-events --query id -o tsv
$hubOut = az eventhubs eventhub show -g $ResourceGroup --namespace-name $EventHubs -n match-broadcast --query id -o tsv
$st = az storage account show -g $ResourceGroup -n $Storage --query id -o tsv
$foundry = az cognitiveservices account show -g $ResourceGroup -n $FoundryResource --query id -o tsv
function Grant($principal, $role, $scope) {
    az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal --role $role --scope $scope -o none
}

# Ingest gateway: Azure Functions (Flex Consumption)
az functionapp create -g $ResourceGroup -n $Function --storage-account $Storage --flexconsumption-location $Location `
    --runtime python --runtime-version 3.12 --assign-identity "[system]" --app-insights $AppInsights -o none
az functionapp config appsettings set -g $ResourceGroup -n $Function --settings "EVENTHUB_NAMESPACE=$fqns" "EVENTHUB_NAME=match-events" -o none
Grant (az functionapp identity show -g $ResourceGroup -n $Function --query principalId -o tsv) "Azure Event Hubs Data Sender" $hubIn
Compress-Archive -Path functions/ingest/function_app.py, functions/ingest/host.json, functions/ingest/requirements.txt -DestinationPath "$env:TEMP/ingest.zip" -Force
az functionapp deployment source config-zip -g $ResourceGroup -n $Function --src "$env:TEMP/ingest.zip" --build-remote true -o none

# Insights worker: one agent run per match, always-on single replica (KEDA rule kept as a safety net)
az acr build -r $Registry -t "player:$Tag" --no-logs . -o none
$endpoint = "https://$FoundryResource.services.ai.azure.com/api/projects/$FoundryProject"
az containerapp create -n $Worker -g $ResourceGroup --environment $Environment --image "$Registry.azurecr.io/player:$Tag" `
    --registry-server "$Registry.azurecr.io" --registry-identity system --system-assigned --command insights-worker `
    --min-replicas 1 --max-replicas 1 --cpu 0.25 --memory 0.5Gi --secrets "appi=$appi" `
    --env-vars "EVENTHUB_NAMESPACE=$fqns" EVENTHUB_IN=match-events EVENTHUB_OUT=match-broadcast `
               "CHECKPOINT_ACCOUNT_URL=https://$Storage.blob.core.windows.net" CHECKPOINT_CONTAINER=checkpoints `
               FOUNDRY_PROJECT_ENDPOINT=$endpoint FOUNDRY_MODEL=$Model FOUNDRY_AUTH=default LIVE_FOUNDRY_MAX=2 `
               APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appi `
    --scale-rule-name eventhub-backlog --scale-rule-type azure-eventhub --scale-rule-identity system `
    --scale-rule-metadata 'consumerGroup=$Default' unprocessedEventThreshold=1 "eventHubNamespace=$EventHubs" eventHubName=match-events `
                          "storageAccountName=$Storage" blobContainer=checkpoints checkpointStrategy=blobMetadata -o none
$w = az containerapp show -n $Worker -g $ResourceGroup --query identity.principalId -o tsv
Grant $w "Azure Event Hubs Data Receiver" $hubIn
Grant $w "Azure Event Hubs Data Sender" $hubOut
Grant $w "Storage Blob Data Contributor" $st
Grant $w "Foundry User" $foundry

# Player: reads the broadcast and can kick off sample matches through the Function
$p = az containerapp show -n $Player -g $ResourceGroup --query identity.principalId -o tsv
Grant $p "Azure Event Hubs Data Receiver" $hubOut
$key = az functionapp function keys list -g $ResourceGroup -n $Function --function-name ingest --query default -o tsv
az containerapp secret set -n $Player -g $ResourceGroup --secrets "feedurl=https://$Function.azurewebsites.net/api/ingest?code=$key" "appi=$appi" -o none
az containerapp update -n $Player -g $ResourceGroup --image "$Registry.azurecr.io/player:$Tag" `
    --set-env-vars "EVENTHUB_NAMESPACE=$fqns" EVENTHUB_OUT=match-broadcast FEED_INGEST_URL=secretref:feedurl `
                   APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appi -o none
# MCP server (read-only, scale to zero) and the player's studio agent pointing at it
az containerapp create -n insidethegame-mcp -g $ResourceGroup --environment $Environment --image "$Registry.azurecr.io/player:$Tag" `
    --registry-server "$Registry.azurecr.io" --registry-identity system --system-assigned --command insights-mcp `
    --ingress external --target-port 8001 --min-replicas 0 --max-replicas 2 --cpu 0.25 --memory 0.5Gi `
    --env-vars MCP_TRANSPORT=http HOST=0.0.0.0 PORT=8001 -o none
$mcp = az containerapp show -n insidethegame-mcp -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
az containerapp update -n $Player -g $ResourceGroup --set-env-vars "MCP_URL=https://$mcp/mcp" -o none

Write-Host "Done. Connect $AppInsights to the Foundry project (Foundry portal > Tracing) to see agent traces there."
