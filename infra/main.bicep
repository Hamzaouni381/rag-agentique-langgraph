// Infrastructure Azure du RAG agentique. Guide pas a pas : doc/11_deploiement_azure.md
//
//   Internet --> Container App (0 a 1 replique)
//                  |-- conteneur api    (API + interface)
//                  |-- conteneur ollama (LLM, joint sur localhost)
//                  |--> PostgreSQL Flexible Server (pgvector, tables, memoire)
//                  '--> Azure Files (documents deposes, /app/data)

@description('Region autorisee par l\'abonnement ou Postgres n\'est pas restreint.')
param location string = 'spaincentral'

@description('Image de l\'API publiee par la CI, ex. ghcr.io/owner/repo:<sha>')
param image string

@description('Image Ollama publiee par la CI, ex. ghcr.io/owner/repo:ollama-<sha>')
param ollamaImage string

@secure()
@description('Mot de passe de l\'administrateur Postgres (role rag).')
param postgresPassword string

@secure()
@description('Mot de passe du role lecture seule rag_reader (outil SQL).')
param readerPassword string

@secure()
@description('Cle exigee par l\'API dans l\'en-tete X-API-Key.')
param apiKey string

var suffix = uniqueString(resourceGroup().id)

// --- PostgreSQL ---
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: 'pg-rag-${suffix}'
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: 'rag'
    administratorLoginPassword: postgresPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// Les sous-ressources du serveur sont chainees : Azure refuse (ServerIsBusy) les modifications simultanees.
resource pgVectorAllowed 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'rag'
  dependsOn: [pgVectorAllowed]
}

// 0.0.0.0 = "services Azure" : sans VNet, une Container App n'a pas d'IP sortante fixe.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [database]
}

// --- Azure Files : le disque d'un conteneur est perdu a chaque mise en veille ---
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stragdocs${suffix}'
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource documentsShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'documents'
  properties: {
    shareQuota: 5
  }
}

// --- Container Apps ---
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-rag-${suffix}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('0.2')
    }
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'aca-env-rag'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'documents'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: documentsShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'rag-agentique'
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
      }
      secrets: [
        {
          name: 'database-url'
          value: 'postgresql+psycopg://rag:${postgresPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/${database.name}?sslmode=require'
        }
        {
          name: 'reader-database-url'
          value: 'postgresql+psycopg://rag_reader:${readerPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/${database.name}?sslmode=require'
        }
        {
          name: 'api-key'
          value: apiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          resources: {
            cpu: json('2.0') // pic mesure a 1,9 Gio en conteneur, modeles charges
            memory: '4Gi'
          }
          env: [
            { name: 'LLM_PROVIDER', value: 'ollama' }
            { name: 'OLLAMA_BASE_URL', value: 'http://localhost:11434' } // meme replica : localhost partage
            { name: 'LLM_NUM_THREAD', value: '2' } // = quota CPU du conteneur ollama (sinon throttling)
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'READER_DATABASE_URL', secretRef: 'reader-database-url' }
            { name: 'API_KEY', secretRef: 'api-key' }
            { name: 'HF_HUB_OFFLINE', value: '1' } // modeles deja dans l'image
            { name: 'RETRIEVE_K', value: '10' } // le rerank sur CPU coute ~0,65 s par candidat a 2 vCPU
          ]
          volumeMounts: [
            {
              volumeName: 'documents'
              mountPath: '/app/data'
            }
          ]
        }
        {
          name: 'ollama'
          image: ollamaImage
          resources: {
            cpu: json('2.0') // 2 + 2 vCPU = maximum d'une replique en Consumption
            memory: '4Gi' // 1,25 Gio mesures, modele charge
          }
        }
      ]
      scale: {
        minReplicas: 0 // aucun cout de calcul au repos
        maxReplicas: 1 // l'index BM25 est en memoire : une seule replique pour rester coherent
      }
      volumes: [
        {
          name: 'documents'
          storageType: 'AzureFile'
          storageName: environmentStorage.name
        }
      ]
    }
  }
  dependsOn: [allowAzureServices]
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output postgresName string = postgres.name
output postgresHost string = postgres.properties.fullyQualifiedDomainName
