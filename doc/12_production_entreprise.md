# 12. Vers un système de production d'entreprise

Ce chapitre dit ce que le système a déjà d'un vrai système de production, ce qui lui manque et ce qui reste hors de portée sans budget. La contrainte est assumée : peu de ressources, CPU, réponses lentes.

**La position à défendre :** l'architecture et les pratiques sont celles de la production, la capacité est petite. Monter en charge consiste à changer la taille des ressources et le modèle, pas à refaire le système.

## 12.1 Où en est-on

| Domaine | État | Ce qui existe | Ce qui manque |
|---|---|---|---|
| **Livraison** | Partiel | Infrastructure en Bicep, images construites par la CI, tests à chaque push, mise à jour sans coupure | Déploiement automatique, préproduction, retour arrière documenté |
| **Exploitation** | Fait | File d'attente, refus explicite si saturation, délais maximum, sondes, arrêt progressif | Alertes |
| **Traçabilité** | Partiel | Journal d'audit en base et en JSON, identifiant par requête | Identité de l'utilisateur, traces Langfuse actives sur Azure |
| **Identité et droits** | À faire | Une clé API partagée | Connexion par compte, rôles, documents cloisonnés |
| **Sécurité** | Partiel | HTTPS, SQL en lecture seule à trois barrières, secrets hors du dépôt, taille des fichiers limitée | Key Vault, base sur réseau privé, défense contre l'injection de prompt |
| **Qualité IA** | Partiel | Script d'évaluation, refus sans appel au LLM si rien n'est pertinent | Modèle choisi par mesure, contrôle des réponses, évaluation en CI, retours utilisateurs |
| **Données** | Partiel | Sauvegardes de 7 jours, conservation de l'audit limitée à 90 jours | Test de restauration, purge des conversations |
| **Haute disponibilité** | Hors budget | Reprise automatique du conteneur | Plusieurs répliques, base en haute disponibilité |

## 12.2 Ce qui est mesuré

Mesures du 03/11/2026 sur Azure : 4 vCPU au total, sans GPU, corpus de démonstration de 3 fichiers.

| Mesure | Résultat |
|---|---|
| 10 questions lancées en même temps | **10 réussites sur 10** en 54 s : attente médiane 29 s, maximale 54 s |
| Questions envoyées en continu pendant une mise à jour | **64 sur 64 en HTTP 200**, aucune coupure, sur un seul essai |
| Question seule, application réveillée | 2 à 8 s |
| Résumé d'un petit document | 7 à 20 s |

**Ce que ces chiffres ne prouvent pas :** le corpus est minuscule et les questions se répétaient. Avec de vrais documents, chaque question prendra plus de temps, et l'attente totale augmentera d'autant. Au-delà de 180 s d'attente ou de 20 demandes en file, le service refuse la demande avec un code 503.

## 12.3 Défauts connus

- **Refus à tort.** « Combien de temps sont conservés les objets trouvés ? » répond que l'information est absente, alors que le règlement l'indique (article 9). À traiter au lot 3, en mesurant l'effet du seuil de pertinence.
- **Résumés à relire.** Le petit modèle peut déformer un terme : voir la section « État actuel » du [chapitre 11](11_deploiement_azure.md).
- **Journaux non corrélés.** Les lignes de l'audit portent un identifiant de requête, mais pas les lignes internes (routage, appels au LLM). Avec plusieurs utilisateurs, seule la ligne d'audit relie une question à son résultat.

## 12.4 Le plan

| Lot | Contenu | Coût Azure en plus | État |
|---|---|---|---|
| **1. Exploitation** | File d'attente, audit, mise à jour sans coupure, délais maximum | Quasi nul | **Fait** |
| **2. Identité** | Connexion par compte Microsoft, rôles, audit nominatif, documents cloisonnés | Aucun | En cours : code prêt hors cloisonnement des documents, non déployé |
| **3. Qualité** | Modèle CPU choisi par mesure, contrôle des réponses, évaluation en CI, retours utilisateurs | Faible | À faire |
| **4. Sécurité** | Key Vault, identité managée, réseau privé, défense contre l'injection de prompt | Faible | À faire |
| **5. Livraison** | Déploiement automatique, préproduction, retour arrière, alertes, test de restauration | Quasi nul | À faire |

**Hors de portée avec le crédit étudiant :** une réplique toujours active, plusieurs répliques et une base en haute disponibilité. Au tarif actif de 0,60 $ de l'heure, une seule réplique sollicitée en permanence coûterait environ 440 $ par mois, soit plus que le crédit total. Le tarif au repos est plus bas, mais je ne l'ai pas chiffré.

## 12.5 Comment fonctionne le lot 1

- **File d'attente** ([app/admission.py](../app/admission.py)). Le LLM sur CPU traite efficacement une demande à la fois. Les suivantes attendent leur tour ; l'ingestion passe par la même file, car elle sollicite aussi le CPU. Réglages : `MAX_ACTIVE_REQUESTS`, `REQUEST_QUEUE_SIZE`, `REQUEST_WAIT_SECONDS`.
- **Audit** ([app/audit.py](../app/audit.py)). Chaque question, dépôt et suppression est enregistré dans la table `audit_log`, consultable avec `GET /api/audit`. Le rôle lecture seule de l'outil SQL n'y a pas accès.
- **Sondes.** `/api/live` dit si le processus répond, `/api/ready` dit si les modèles sont chargés. Azure n'envoie du trafic à une nouvelle version qu'une fois celle-ci prête.
- **Arrêt progressif.** Lors d'une mise à jour, l'ancienne version a 120 s pour finir ses réponses. Ollama ignore le signal d'arrêt pendant ce délai, sinon il couperait les réponses en cours.

## 12.6 En entretien

- *« Ce système tiendrait-il en production ? »* Pas à fort trafic : il traite une question à la fois sur CPU. Il a en revanche ce que la production exige : file d'attente, audit, mise à jour sans coupure. J'ai mesuré 10 demandes simultanées, et je sais ce qui se passe au-delà : un refus explicite, pas une panne.
- *« Pourquoi une file plutôt que traiter en parallèle ? »* Sur 2 vCPU, des générations simultanées se partageraient le même CPU : toutes ralentiraient, et elles risqueraient d'atteindre ensemble la limite de 240 s de l'entrée.
- *« Que changerais-tu avec du budget ? »* Un LLM managé ou un GPU, plusieurs répliques avec la recherche lexicale dans Postgres, et une base en haute disponibilité. Le code et le pipeline de livraison restent les mêmes.
