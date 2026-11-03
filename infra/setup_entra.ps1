# Prepare la connexion Microsoft Entra : application, roles admin/user, role admin de la personne connectee.
# A lancer une fois, par un administrateur de l'annuaire, depuis la racine du projet :
#   powershell -ExecutionPolicy Bypass -File infra\setup_entra.ps1
# Relance sans risque. Le secret part dans infra\.secrets.ps1 (ignore par git) et n'est jamais affiche.
$nom = 'rag-agentique'
$fqdn = 'rag-agentique.redground-2b9d1e76.spaincentral.azurecontainerapps.io'
$annuaireAttendu = 'Default Directory'
$secrets = Join-Path $PSScriptRoot '.secrets.ps1'
$roleAdmin = '6f1f4c1e-9d3b-4a56-8e21-3b7a2c1d0a01'
$roleUser = '6f1f4c1e-9d3b-4a56-8e21-3b7a2c1d0a02'

$annuaire = az rest --method get --url 'https://graph.microsoft.com/v1.0/organization?$select=displayName' --query 'value[0].displayName' -o tsv
if ($annuaire -ne $annuaireAttendu) { throw "Annuaire inattendu : '$annuaire'. Rien n'a ete cree." }
"Annuaire : $annuaire"
$tenant = az account show --query tenantId -o tsv

$roles = @(
  @{ id = $roleAdmin; value = 'admin'; displayName = 'Administrateur'; description = 'Pose des questions, gere les documents, lit le journal d audit'; allowedMemberTypes = @('User'); isEnabled = $true },
  @{ id = $roleUser; value = 'user'; displayName = 'Utilisateur'; description = 'Pose des questions et consulte la liste des documents'; allowedMemberTypes = @('User'); isEnabled = $true }
)
$rolesFile = New-TemporaryFile
[IO.File]::WriteAllText($rolesFile, (ConvertTo-Json -InputObject $roles -Depth 4))

$appId = az ad app list --display-name $nom --query '[0].appId' -o tsv
if (-not $appId) {
  $appId = az ad app create --display-name $nom --sign-in-audience AzureADMyOrg --web-redirect-uris "https://$fqdn/.auth/login/aad/callback" --enable-id-token-issuance true --app-roles "@$rolesFile" --query appId -o tsv
}
Remove-Item $rolesFile
if (-not $appId) { throw "Creation de l'application en echec." }
"Application : $appId"

$spId = az ad sp list --filter "appId eq '$appId'" --query '[0].id' -o tsv
if (-not $spId) { $spId = az ad sp create --id $appId --query id -o tsv }
if (-not $spId) { throw "Creation du principal de service en echec." }
# Seules les personnes a qui un role a ete attribue peuvent se connecter.
az ad sp update --id $spId --set appRoleAssignmentRequired=true | Out-Null

$moi = az ad signed-in-user show --query id -o tsv
$url = "https://graph.microsoft.com/v1.0/servicePrincipals/$spId/appRoleAssignedTo"
$deja = az rest --method get --url $url --query "value[?principalId=='$moi'] | length(@)" -o tsv
if ($deja -eq '0') {
  $body = New-TemporaryFile
  [IO.File]::WriteAllText($body, (@{ principalId = $moi; resourceId = $spId; appRoleId = $roleAdmin } | ConvertTo-Json))
  # L'application vient d'etre creee : Entra peut mettre quelques secondes a la voir.
  for ($i = 0; $i -lt 6; $i++) {
    az rest --method post --url $url --headers 'Content-Type=application/json' --body "@$body" -o none 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 10
  }
  $codeRole = $LASTEXITCODE
  Remove-Item $body
  if ($codeRole -ne 0) { throw "Attribution du role admin en echec." }
}
"Role admin : attribue"

if (-not (Select-String -Path $secrets -Pattern 'AUTH_CLIENT_SECRET' -Quiet)) {
  $secret = az ad app credential reset --id $appId --append --display-name 'container-app' --years 1 --query password -o tsv 2>$null
  if (-not $secret) { throw "Creation du secret en echec." }
  Add-Content -Path $secrets -Encoding utf8 -Value "`$AUTH_CLIENT_ID = '$appId'`n`$AUTH_TENANT_ID = '$tenant'`n`$AUTH_CLIENT_SECRET = '$secret'"
  Remove-Variable secret
}
"Secret : enregistre dans infra\.secrets.ps1"
"TERMINE"
