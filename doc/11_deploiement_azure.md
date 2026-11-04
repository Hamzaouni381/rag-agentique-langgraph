# 11. Déploiement sur Azure (Azure for Students)

Ce chapitre se suit commande par commande, dans **PowerShell, à la racine du projet**. Chaque étape dit ce qu'elle fait, pourquoi, et comment vérifier le résultat.

## État actuel

Section tenue à jour à chaque intervention. N'y figure que ce qui a été vérifié.

| Date | Élément | État |
|---|---|---|
| 03/11/2026 | Application | **En ligne** : [rag-agentique.redground-2b9d1e76.spaincentral.azurecontainerapps.io](https://rag-agentique.redground-2b9d1e76.spaincentral.azurecontainerapps.io) |
| 03/11/2026 | Azure | Groupe `rg-rag-agentique`, région `spaincentral`, déploiement `Succeeded`, base initialisée, pare-feu refermé |
| 04/11/2026 | Images déployées | Tags `256741d4…` (API) et `ollama-256741d4…` (Ollama), révision `rag-agentique--0000008`. Valeur à jour : `az containerapp show -g rg-rag-agentique -n rag-agentique --query "properties.template.containers[].image"` |
| 03/11/2026 | LLM | Ollama `qwen2.5:1.5b` dans un conteneur voisin de l'API, à la place d'Azure OpenAI (bloqué par `715-123420`) |
| 03/11/2026 | Tests en production | Santé OK, `401` sans clé, 3 fichiers de `samples/` déposés en 9 s, questions documents et SQL correctes, streaming OK |
| 03/11/2026 | Temps de réponse mesurés | Application déjà réveillée : 3 à 8 s pour une question, 8 à 20 s pour un résumé ; première question après chargement des modèles : 17 s |
| 03/11/2026 | Résumés | En ligne et testés : « résumé le document reglement T2 » répond, et une demande sans document identifiable demande lequel résumer |
| 03/11/2026 | Défaut constaté | Le modèle déforme parfois un résumé : sur un essai, « avoir de 50 % » est devenu « réduction de 50 % », et « sur demande sous 60 jours » est devenu « demande d'explication » |
| 03/11/2026 | 10 questions simultanées | 10 réussites sur 10 en 54 s, attente maximale 54 s ([chapitre 12](12_production_entreprise.md)) |
| 03/11/2026 | Mise à jour pendant l'utilisation | 64 questions envoyées en continu, 64 réponses HTTP 200, sur un seul essai |
| 04/11/2026 | Connexion par compte (lot 2) | Déployée. Vérifié sur le site : `401` sans identité, `401` avec un faux en-tête d'identité, redirection vers le bon annuaire. Connexion réussie dans une fenêtre privée. **Reste à vérifier :** une question enregistrée sous le nom de la personne, et la déconnexion |
| 04/11/2026 | Ancienne clé dans un navigateur | Un navigateur qui gardait la clé entrait sans connexion : corrigé. S'il garde aussi l'ancienne version de la page, vider les données du site |
| 04/11/2026 | Alertes | 4 alertes actives : erreurs 5xx, redémarrages, base injoignable, disque de la base. **Réception de l'e-mail non vérifiée** : Azure refuse l'envoi de test sur un abonnement gratuit (`Free subscription not supported`) |
| 04/11/2026 | Retour arrière | Testé en production : ancienne version en service après 104 s, retour à la version courante après 70 s, 51 questions sur 51 en HTTP 200 |
| 04/11/2026 | Isolation par utilisateur | En ligne, révision `rag-agentique--0000008`. Vérifié sur le PostgreSQL d'Azure : RLS active, 5 passages pour le bon propriétaire, 0 pour un autre ou sans propriétaire ([chapitre 12](12_production_entreprise.md)). **Reste à vérifier :** deux vraies personnes connectées |
| 04/11/2026 | Corpus de démonstration | Les 3 fichiers de `samples/` sont maintenant dans l'espace du compte de service. Chaque personne connectée part d'un espace vide |
| 03/11/2026 | Non mesuré | Le réveil complet après mise en veille |

## 11.1 Ce qu'on déploie

```
Internet ──HTTPS──► Container App "rag-agentique"     (0 à 1 réplique, images ghcr.io publiées par la CI)
                      │  ├─ conteneur api    (API + interface, 2 vCPU, 4 Gio)
                      │  └─ conteneur ollama (qwen2.5:1.5b, 2 vCPU, 4 Gio, joint sur localhost)
                      ├──► PostgreSQL Flexible Server (pgvector + tables + mémoire LangGraph)
                      └──► Azure Files                (documents déposés, /app/data)
```

Tout est décrit dans [infra/main.bicep](../infra/main.bicep) : c'est de l'**Infrastructure as Code**. Une seule commande crée l'ensemble, et on peut la relancer sans créer de doublon.

| Choix | Pourquoi | Pourquoi pas l'alternative |
|---|---|---|
| **Container Apps**, 0 à 1 réplique | Aucun coût de calcul au repos. Une seule réplique par choix de coût : le code en supporte plusieurs. | App Service : facturé en continu. Une VM : à administrer soi-même. |
| **`environmentMode: WorkloadProfiles`** | Azure Files et les conteneurs voisins en ont besoin. | Sans ce réglage, Azure crée un environnement **Express**, qui n'accepte ni l'un ni l'autre. La valeur `Standard` est refusée. |
| **PostgreSQL managé** | pgvector disponible, sauvegardes incluses, même code qu'en local. | Postgres dans un conteneur : données perdues à la mise en veille, et Azure Files (SMB) ne convient pas à un moteur de base de données. |
| **Ollama, `qwen2.5:1.5b`, en sidecar** | Aucune dépendance à Azure OpenAI, dont le déploiement a été bloqué (`715-123420`). Même modèle et même code qu'en local. Les données restent dans le conteneur. | Azure OpenAI `gpt-4.1-mini` : bien plus rapide et moins cher à l'usage, mais bloqué sur cet abonnement au 02/11/2026. Ollama sur le PC exposé par un tunnel : l'application dépendrait d'un PC allumé. |
| **ghcr.io** pour l'image | Gratuit, construit par GitHub : pas d'envoi lourd depuis le PC. | ACR : 5 $/mois, et `az acr build` est interdit sur les abonnements étudiants. |
| **Azure Files** | Le disque d'un conteneur disparaît à chaque mise en veille. Sans volume, un `cleanup=full` sur un dossier vide viderait l'index. | Stocker les fichiers dans Postgres : demande de réécrire l'ingestion. |
| **`spaincentral`** | Autorisée par l'abonnement, Postgres non restreint, quota LLM présent. | `germanywestcentral` : Postgres y est restreint. `belgiumcentral` : modèles OpenAI indisponibles. |

## 11.2 Le coût

Prix de détail `spaincentral`, relevés en novembre 2026, calculés pour 730 h par mois.

| Ressource | Coût | Comment le réduire |
|---|---|---|
| Postgres B1ms (calcul) | 14,53 $/mois | Arrêter le serveur hors démo (section 11.9) |
| Postgres (32 Go de stockage) | 4,38 $/mois | Facturé même serveur arrêté |
| Container Apps (4 vCPU, 8 Gio au total) | 0 $ au repos, 0,60 $/h actif | Offre gratuite mensuelle d'environ 12,5 h actives à ce dimensionnement |
| Azure Files, journaux | Moins de 1 $/mois | Journaux plafonnés à 0,2 Go/jour |
| LLM (Ollama) | Inclus dans Container Apps | Pas de facturation au token |

**Total : environ 20 $/mois si la base tourne en permanence**, tant que l'usage reste dans les 12,5 h actives gratuites, soit 5 mois pour 100 $ de crédit. Chaque réveil compte environ 5 minutes d'activité en plus après la dernière requête. Regarde ton solde réel sur [microsoftazuresponsorships.com/Balance](https://www.microsoftazuresponsorships.com/Balance). Le poste qui compte est le calcul Postgres : c'est lui qu'il faut couper.

## 11.3 Étape 1 : vérifier le compte Azure

```powershell
az login                                  # inutile si la commande suivante répond déjà
az account show --query "{nom:name, etat:state}" -o table
az provider register -n Microsoft.DBforPostgreSQL --wait
```

- **`az provider register`** autorise l'abonnement à utiliser un service. C'est gratuit et sans effet si c'est déjà fait.
- **Attendu** : `Azure for Students` à l'état `Enabled`.

Vérifie aussi que les régions autorisées n'ont pas changé : `spaincentral` doit figurer dans la liste.

```powershell
az policy assignment list --query "[].parameters.listOfAllowedLocations.value" -o json
```

## 11.4 Étape 2 : publier l'image Docker

La CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) construit les deux images et les publie sur ghcr.io à chaque push sur `main`. Elle s'authentifie avec le `GITHUB_TOKEN` fourni par GitHub : aucun secret à créer. Le build a lieu chez GitHub, ce qui évite les transferts volumineux sur un réseau domestique instable.

- **Image API** : tag `<sha>`, construite par le [Dockerfile](../Dockerfile).
- **Image Ollama** : tag `ollama-<sha>`, construite par [Dockerfile.ollama](../Dockerfile.ollama). Elle contient le modèle, retire les bibliothèques GPU inutiles et échoue au build si le modèle ne répond pas.

```powershell
git status                                # relis la liste avant de continuer
git push origin main
```

Suis l'exécution dans l'onglet **Actions** du dépôt, puis passe à la suite quand le job `docker` est vert.

**Visibilité : déjà faite.** Le paquet ghcr est public, et les deux images en font partie : Azure les télécharge sans identifiants. Elles ne contiennent que le code (déjà public) et des modèles ouverts : `.env` et `data/` sont exclus par `.dockerignore`, et les secrets sont fournis à l'exécution.

**Vérifier :**

```powershell
$SHA = git rev-parse HEAD
$IMAGE = "ghcr.io/hamzaouni381/rag-agentique-langgraph:$SHA"
$OLLAMA_IMAGE = "ghcr.io/hamzaouni381/rag-agentique-langgraph:ollama-$SHA"
docker manifest inspect $IMAGE            # doit afficher du JSON, sans "unauthorized"
docker manifest inspect $OLLAMA_IMAGE     # idem
```

## 11.5 Étape 3 : créer les secrets

Quatre secrets, générés par le système et conservés dans un fichier ignoré par Git. Le préfixe `Rag` satisfait la règle de complexité d'Azure (majuscule, minuscule, chiffre), et l'hexadécimal évite tout caractère à échapper dans une URL de connexion.

```powershell
function New-Secret {
  $b = New-Object byte[] 24
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  -join ($b | ForEach-Object { $_.ToString('x2') })
}

if (-not (Test-Path infra\.secrets.ps1)) {
  "`$PG_PASSWORD = 'Rag$(New-Secret)'`n`$READER_PASSWORD = 'Rag$(New-Secret)'`n`$APP_DB_PASSWORD = 'Rag$(New-Secret)'`n`$API_KEY = '$(New-Secret)'" |
    Out-File infra\.secrets.ps1 -Encoding utf8
}
Set-ExecutionPolicy -Scope Process Bypass -Force   # pour cette fenêtre seulement
. .\infra\.secrets.ps1
git check-ignore infra\.secrets.ps1                # doit réafficher le chemin : le fichier est bien ignoré
```

Dans chaque **nouvelle fenêtre** PowerShell, recharge ces variables : c'est le piège classique, une variable vide ne provoque pas toujours d'erreur claire.

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; . .\infra\.secrets.ps1
$RG = 'rg-rag-agentique'
```

## 11.6 Étape 4 : créer l'infrastructure

```powershell
$RG = 'rg-rag-agentique'
az group create -n $RG -l spaincentral

$params = @("image=$IMAGE", "ollamaImage=$OLLAMA_IMAGE", "postgresPassword=$PG_PASSWORD", "readerPassword=$READER_PASSWORD", "appDbPassword=$APP_DB_PASSWORD", "apiKey=$API_KEY", "authClientId=$AUTH_CLIENT_ID", "authClientSecret=$AUTH_CLIENT_SECRET", "alertEmail=$(az account show --query user.name -o tsv)")

# Simulation : affiche ce qui serait créé, sans rien créer ni facturer
az deployment group what-if -g $RG -f infra\main.bicep -p $params

# Création réelle : environ 10 minutes
az deployment group create -g $RG -n rag -f infra\main.bicep -p $params --query properties.provisioningState -o tsv
```

- **Groupe de ressources** : un dossier logique. Le supprimer supprime tout son contenu.
- **`what-if`** : équivalent d'un `terraform plan`. Attendu sur un abonnement vide : 17 ressources en `Create` (11 pour l'application, 1 pour la connexion, 5 pour les alertes).
- **Attendu après la création** : `Succeeded`.

Le déploiement s'exécute côté Azure. Si ta connexion coupe, il continue : ne relance rien avant d'avoir vérifié son état.

```powershell
az deployment group show -g $RG -n rag --query properties.provisioningState -o tsv
```

**Récupère les valeurs produites** (à refaire dans chaque nouvelle fenêtre) :

```powershell
$out = az deployment group show -g $RG -n rag --query properties.outputs -o json | ConvertFrom-Json
$URL = $out.appUrl.value
$PG_HOST = $out.postgresHost.value
$PG_NAME = $out.postgresName.value
$URL
```

## 11.7 Étape 5 : préparer la base

[infra/init_db.sql](../infra/init_db.sql) active pgvector et crée les rôles `rag_app` et `rag_reader`. L'application crée ensuite elle-même la Row-Level Security et, à la première visite de chaque personne, son schéma et son rôle ([app/tenancy.py](../app/tenancy.py)).

Le pare-feu de Postgres bloque ton PC. On ouvre ton adresse le temps du script, puis on referme. `psql` tourne dans un conteneur, donc rien à installer, mais **Docker Desktop doit être lancé**.

```powershell
$MY_IP = Invoke-RestMethod https://api.ipify.org
az postgres flexible-server firewall-rule create -g $RG --server-name $PG_NAME --name mon-pc --start-ip-address $MY_IP --end-ip-address $MY_IP

Get-Content infra\init_db.sql -Raw | docker run --rm -i -e PGPASSWORD=$PG_PASSWORD pgvector/pgvector:pg16 psql "host=$PG_HOST port=5432 dbname=rag user=rag sslmode=require" -v "reader_password=$READER_PASSWORD" -v "app_password=$APP_DB_PASSWORD"

az postgres flexible-server firewall-rule delete -g $RG --server-name $PG_NAME --name mon-pc --yes
```

**Attendu** : `CREATE EXTENSION`, `CREATE ROLE`, `ALTER ROLE`, `GRANT`, sans `ERROR`.

**Si le port 5432 est bloqué par ton réseau** : passe par [Azure Cloud Shell](https://shell.azure.com) (Bash), où `psql` est déjà installé et où la règle « services Azure » s'applique. Tu n'y as pas besoin de la règle `mon-pc` :

```bash
git clone https://github.com/Hamzaouni381/rag-agentique-langgraph.git && cd rag-agentique-langgraph
PGPASSWORD='<PG_PASSWORD>' psql "host=<PG_HOST> port=5432 dbname=rag user=rag sslmode=require" -v reader_password='<READER_PASSWORD>' -v app_password='<APP_DB_PASSWORD>' -f infra/init_db.sql
```

## 11.8 Étape 6 : tester de bout en bout

La première requête réveille le conteneur : compte **1 à 3 minutes**. Azure la retient pendant le démarrage (image à télécharger, puis Python).

```powershell
# 1. Santé : "database" doit valoir "ok" et "llm" "ollama"
curl.exe -s --max-time 240 "$URL/api/health"

# 2. Sécurité : sans clé, on doit recevoir 401
curl.exe -s -o NUL -w "%{http_code}`n" "$URL/api/documents"

# 3. Corpus de démonstration : la réponse indique le nombre de chunks et les tables créées
curl.exe -s --max-time 240 -H "X-API-Key: $API_KEY" -F "files=@samples/reglement_T2.pdf" -F "files=@samples/procedure_PV300.docx" -F "files=@samples/incidents_septembre.xlsx" "$URL/api/upload"

# 4. Une question documentaire et une question SQL
function Ask($q) {
  $body = @{ question = $q } | ConvertTo-Json
  Invoke-RestMethod "$URL/api/ask" -Method Post -Headers @{ 'X-API-Key' = $API_KEY } -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 240
}
Ask "Quel est l'indice de protection du portique PV-300 ?"
Ask "Quel est le retard total cumule sur la ligne T2 ?"
```

**Attendu** : une route `documents` avec des sources pour la première question, une route `database` avec la requête générée pour la seconde. Des accents déformés (`supÃ©rieur`) dans PowerShell 5.1 viennent de l'affichage de `Invoke-RestMethod`, pas de l'application.

Ouvre ensuite `$URL` dans le navigateur et connecte-toi avec ton compte Microsoft (section suivante). La clé `$API_KEY` sert aux scripts et aux tests : elle est enregistrée dans l'audit sous le nom `service:cle-api`.

### La connexion par compte

**Mise en place, une seule fois**, par un administrateur de l'annuaire. Le script vérifie l'annuaire, crée l'application `rag-agentique` et ses rôles, et attribue le rôle admin à la personne qui le lance. Le déploiement de l'étape 4 lit ensuite `$AUTH_CLIENT_ID` et `$AUTH_CLIENT_SECRET` dans `infra\.secrets.ps1`.

```
powershell -ExecutionPolicy Bypass -File infra\setup_entra.ps1
```

**Ajouter une personne :**

1. Portail Azure, **Microsoft Entra ID**, **Utilisateurs**, **Inviter un utilisateur externe**.
2. **Applications d'entreprise**, `rag-agentique`, **Utilisateurs et groupes**, **Ajouter un utilisateur/groupe**, puis choisir le rôle.

Sans rôle attribué, la personne ne peut pas se connecter. Le secret de l'application expire au bout d'un an : relancer `az ad app credential reset`, mettre à jour `infra\.secrets.ps1`, puis redéployer.

| Rôle | Droits |
|---|---|
| `admin` | Questions, ajout et suppression de documents, journal d'audit |
| `user` | Questions et liste des documents |

**Pourquoi l'en-tête d'identité est sûr :** `X-MS-CLIENT-PRINCIPAL` n'est lu qu'avec `AUTH_MODE=easyauth`. Devant l'application, la plateforme ne laisse pas passer un en-tête forgé : testé sur le site, il reçoit un `401`.

**Dépose uniquement les fichiers de `samples/` :** l'application est sur Internet, et les fichiers de `data/documents` en local peuvent être personnels.

## 11.9 Exploitation au quotidien

| Besoin | Commande |
|---|---|
| Publier une nouvelle version | `git push`, attendre la CI, puis `az containerapp update -g $RG -n rag-agentique --container-name api --image "ghcr.io/hamzaouni381/rag-agentique-langgraph:$(git rev-parse HEAD)"` |
| Voir les journaux | Portail Azure, Container App, **Journaux**, avec la requête `union isfuzzy=true ContainerAppConsoleLogs_CL, ContainerAppConsoleLogs \| where TimeGenerated > ago(1h) \| order by TimeGenerated asc`. Compter quelques minutes de retard. `az containerapp logs show` plante sous Windows (`UnicodeEncodeError`) sur les barres de progression du chargement des modèles, même avec `PYTHONUTF8=1` |
| État des révisions | `az containerapp revision list -g $RG -n rag-agentique -o table` |
| Lire le journal d'audit | `curl.exe -s -H "X-API-Key: $API_KEY" "$URL/api/audit?limit=50"` |
| **Couper la base** (économie) | `az postgres flexible-server stop -g $RG -n $PG_NAME` |
| Rallumer la base avant une démo | `az postgres flexible-server start -g $RG -n $PG_NAME` (quelques minutes) |
| Changer la clé API | Modifier `infra/.secrets.ps1`, le recharger, puis relancer `az deployment group create` (étape 4) |
| Tout supprimer | Voir ci-dessous |

> **Un serveur arrêté redémarre seul au bout de 7 jours**, et la facturation du calcul reprend. Pense à le recouper chaque semaine. Base arrêtée, le lien public répond `"database": "error"` : rallume-la **avant** un entretien.

> **Couper la base déclenche l'alerte « base injoignable ».** C'est normal : ce n'est une panne que si tu ne l'as pas coupée toi-même.

> Relancer le Bicep remet les variables d'environnement à l'état du fichier : ajoute toute nouvelle variable (Langfuse par exemple) dans `main.bicep` plutôt qu'à la main dans le portail.

**Tout supprimer et arrêter toute facturation :**

```powershell
az group delete -n $RG --yes
```

## 11.10 Dépannage

| Symptôme | Cause | Solution |
|---|---|---|
| `ExpressEnvironmentResourceNotSupported` | Environnement créé en mode Express, sans `environmentMode` dans le Bicep | Garder `environmentMode: 'WorkloadProfiles'`. Le mode d'un environnement existant ne se change pas : le supprimer avec `az containerapp env delete`, puis relancer l'étape 4 |
| `ManagedEnvironmentModeNotSupported` sur `Standard` | Valeur trompeuse : ce mode n'existe pas sous ce nom | Utiliser `WorkloadProfiles` |
| `503` et « réessayez dans quelques instants » | File d'attente pleine, ou plus de 180 s d'attente | Reposer la question. Si c'est fréquent, voir les réglages au [chapitre 12](12_production_entreprise.md) |
| La nouvelle version reste en `Activating` | Modèles trop longs à charger, ou conteneur en erreur | La sonde de démarrage laisse 4 minutes. Lire les journaux, puis `az containerapp revision list` |
| `ServerIsBusy` pendant le déploiement | Postgres traite encore une opération | Attendre 2 minutes et relancer la même commande : elle reprend là où elle en était |
| `ConnectionResetError`, `broken pipe` | Réseau local | L'opération a souvent réussi côté Azure : vérifier avec `az deployment group show` avant de relancer |
| Échec du déploiement sur l'image | Image privée ou tag inexistant | Refaire la vérification de l'étape 2 |
| `database` en erreur dans `/api/health` | Base arrêtée, ou mauvais mot de passe | `az postgres flexible-server show -g $RG -n $PG_NAME --query state -o tsv` |
| Les questions SQL ne marchent pas, le reste oui | Étape 5 non faite ou faite après le démarrage du graphe | Refaire l'étape 5, puis `az containerapp revision restart -g $RG -n rag-agentique --revision <nom>` |
| `401` dans l'interface après un changement de clé | Ancienne clé gardée par le navigateur | Recharger : l'interface redemande la clé |
| Dépôt d'un gros fichier coupé vers 4 minutes | Limite de 240 s de l'entrée Container Apps ; l'ingestion est synchrone | Déposer les fichiers un par un |
| Réponse coupée ou `504` sur `/api/ask` vers 4 minutes | Limite de 240 s de l'entrée Container Apps, atteinte par un long contexte sur CPU | Utiliser l'interface, qui passe par `/api/ask/stream` et affiche la réponse au fil de l'eau |
| Erreur `connection refused` vers Ollama | Conteneur `ollama` en cours de démarrage ou arrêté | `az containerapp logs show -g $RG -n rag-agentique --container ollama --tail 50`, puis réessayer |
| `model not found` | Image Ollama sans le modèle attendu | `OLLAMA_MODEL` et [Dockerfile.ollama](../Dockerfile.ollama) doivent nommer le même modèle |

## 11.11 Limites à connaître (et à dire en entretien)

- **La base est joignable depuis tout service Azure** (règle `0.0.0.0`) : seuls le mot de passe et TLS la protègent. La bonne réponse en production est un VNet et un accès privé, écartés ici pour le coût et la complexité.
- **Pas de partage entre personnes** : chacun ne voit que ses documents. Partager un document dans une équipe reste à construire.
- **L'application détient encore le rôle administrateur `rag`** pour créer les schémas et écrire les tables Excel. Sur Azure, ce rôle contourne la RLS : les lectures de passages passent donc toujours par `rag_app`. Le séparer davantage fait partie du lot sécurité.
- **Secret d'application à renouveler chaque année** : une identité managée éviterait cette tâche (lot 4).
- **Démarrage à froid**, prix du scale-to-zero : téléchargement des deux images (1,95 et 2,30 Go compressés), puis environ 40 s pour charger les modèles d'embedding et de rerank et 15 à 20 s pour charger le LLM (mesurés en conteneur limité à 2 vCPU). Réveille l'application et pose une question avant une démo.
- **Le LLM tourne sur CPU.** Sur Azure, les questions du corpus de démonstration répondent en 3 à 7 s, application réveillée (quelques mesures seulement, à consolider avec `eval/run_eval.py`). Un long contexte reste bien plus lent : sur le PC de développement limité à 2 vCPU, 1 966 tokens de contexte et 256 générés prennent 149 s. Des documents plus longs rapprocheront donc les réponses de la limite de 240 s.
- **Petit modèle, résumés à relire.** `qwen2.5:1.5b` peut déformer un terme ou une condition (voir « Défaut constaté » dans l'état actuel). Pour un usage réel, il faudrait un modèle plus capable et une évaluation des résumés.
- **`LLM_NUM_THREAD` doit valoir le quota CPU du conteneur.** Sans ce réglage, Ollama lance trop de threads pour le quota : la même mesure passe de 149 à 174 s.
- **Le rerank tourne sur CPU** : environ 0,65 s par candidat à 2 vCPU. D'où `RETRIEVE_K=10` en cloud, contre 20 en local, au prix d'un rappel potentiellement plus faible : à re-mesurer avec `eval/run_eval.py`.
- **Une seule question à la fois** : les suivantes attendent dans une file. Le fonctionnement et les mesures sont au [chapitre 12](12_production_entreprise.md).

**Questions probables :**

- *« Pourquoi Bicep plutôt que le portail ? »* Le déploiement est reproductible, relu comme du code, et testable à blanc avec `what-if`.
- *« Comment gères-tu les secrets ? »* Jamais dans le dépôt ni dans l'image. Ce sont des secrets Container Apps, injectés en variables d'environnement. La clé Storage est lue directement par le Bicep (`listKeys`), personne ne la copie à la main.
- *« Pourquoi Ollama plutôt qu'Azure OpenAI ? »* Azure OpenAI était bloqué par un contrôle anti-abus sur l'abonnement étudiant. Ollama retire cette dépendance sans changer le code, au prix de la latence. Avec un compte sans blocage, je reprendrais Azure OpenAI : une question y coûte environ 0,001 $ et répond bien plus vite.
- *« Pourquoi une seule réplique ? »* Par choix de coût : chaque réplique coûte 0,60 $/h active. La recherche est dans PostgreSQL, donc passer à plusieurs répliques ne demande qu'un réglage. Chacune aurait toutefois sa propre file d'attente.
- *« Pourquoi les modèles sont-ils dans l'image ? »* Le disque est éphémère : sinon, chaque réveil retéléchargerait 1,6 Go depuis Hugging Face et dépendrait de sa disponibilité.
