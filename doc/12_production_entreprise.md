# 12. Vers un système de production d'entreprise

Ce chapitre dit ce que le système a déjà d'un vrai système de production, ce qui lui manque et ce qui reste hors de portée sans budget. La contrainte est assumée : peu de ressources, CPU, réponses lentes.

**La position à défendre :** l'architecture et les pratiques sont celles de la production, la capacité est petite. Monter en charge consiste à changer la taille des ressources et le modèle, pas à refaire le système.

## 12.1 Où en est-on

| Domaine | État | Ce qui existe | Ce qui manque |
|---|---|---|---|
| **Livraison** | Partiel | Infrastructure en Bicep, images construites par la CI, tests à chaque push, mise à jour sans coupure, retour arrière testé | Déploiement automatique, préproduction |
| **Exploitation** | Fait | File d'attente, refus explicite si saturation, délais maximum, sondes, arrêt progressif, alertes par e-mail | Vérifier la réception d'une vraie alerte |
| **Traçabilité** | Partiel | Journal d'audit nominatif en base et en JSON, identifiant par requête | Traces Langfuse actives sur Azure |
| **Identité et droits** | Partiel | Connexion Microsoft Entra, rôles `admin` et `user`, compte de service, documents, tables et conversations isolés par personne dans PostgreSQL | Vérification avec deux vraies personnes, de l'audit nominatif et de la déconnexion ; partage par équipe |
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
| Retour à la version précédente | **104 s**, puis 70 s pour revenir à la version courante ; 51 questions sur 51 en HTTP 200 |

**Ce que ces chiffres ne prouvent pas :** le corpus est minuscule et les questions se répétaient. Avec de vrais documents, chaque question prendra plus de temps, et l'attente totale augmentera d'autant. Au-delà de 180 s d'attente ou de 20 demandes en file, le service refuse la demande avec un code 503.

## 12.3 L'isolation entre utilisateurs

Chaque personne ne voit que ses documents, ses tableaux et ses conversations. La garantie vient de PostgreSQL : même une erreur dans le code de l'application ne suffit pas à croiser les données.

| Donnée | Protection | Où |
|---|---|---|
| **Identité** | Identifiant Entra immuable, haché. Une adresse e-mail peut changer ou être réattribuée, jamais le propriétaire ne vient de la requête | [app/auth.py](../app/auth.py) |
| **Passages** | Row-Level Security : le rôle `rag_app` ne voit que les lignes de `app.owner_id`, fixé à l'ouverture de la connexion. Le filtre de la recherche est une seconde protection, indépendante | [app/tenancy.py](../app/tenancy.py) |
| **Tableaux Excel** | Un schéma et un rôle par personne. `rag_reader` n'a aucun droit propre : le SQL écrit par le LLM s'exécute sous le rôle de la personne | [app/sql_backend.py](../app/sql_backend.py) |
| **Fichiers** | Un dossier par personne, et un quota de 200 Mo | [app/api.py](../app/api.py) |
| **Index incrémental** | Un espace par personne : le nettoyage complet de l'une ne peut pas toucher l'autre | [app/vectorstore.py](../app/vectorstore.py) |
| **Conversations** | Clé de mémoire préfixée par le propriétaire, et stockage du navigateur séparé par compte | [app/auth.py](../app/auth.py), [App.jsx](../frontend/src/App.jsx) |

**Comment c'est prouvé.** [tests/test_isolation_postgres.py](../tests/test_isolation_postgres.py) attaque un vrai PostgreSQL, à chaque push, avant la construction de l'image : requête sans filtre, connexion sans propriétaire, écriture chez un autre, SQL visant la table d'un autre, exploration du catalogue. Vérifié aussi sur le PostgreSQL d'Azure le 04/11/2026 :

| Tentative sur Azure | Résultat |
|---|---|
| `rag_app`, bon propriétaire | 5 passages sur 5 |
| `rag_app`, autre propriétaire ou aucun | 0 passage |
| `rag_app` désactive la RLS | `must be owner of table` |
| `rag_reader` lit une table sans prendre le rôle de la personne | `permission denied` |
| Rôle d'une personne sur les passages ou l'audit | `permission denied` |

**Pourquoi deux rôles.** Sur Azure, l'administrateur `rag` a l'attribut `rolbypassrls` : il contourne la RLS. Les lectures de passages ne doivent donc jamais passer par lui.

**Limites à connaître :**
- **Administrateurs et audit** : un administrateur lit le texte des questions de tout le monde dans le journal d'audit, c'est sa fonction. En entreprise, ce droit doit être encadré et annoncé aux utilisateurs.
- **Rôle `rag`** : l'application le détient encore pour créer les schémas et écrire les tables Excel.
- **Noms des tables** : le catalogue PostgreSQL reste lisible par tout rôle. C'est le filtre du SQL généré qui l'interdit, pas les droits de la base.
- **Non vérifié avec deux vraies personnes** : testé de bout en bout en local avec deux identités simulées, mais pas encore avec deux comptes Microsoft réels.
- **Pas de partage** : un document ne peut pas être partagé dans une équipe.
- **Droit à l'effacement** : pas encore de suppression en un geste de toutes les données d'une personne.

## 12.4 Défauts connus

- **Refus à tort.** « Combien de temps sont conservés les objets trouvés ? » répond que l'information est absente, alors que le règlement l'indique (article 9). À traiter au lot 3, en mesurant l'effet du seuil de pertinence.
- **Résumés à relire.** Le petit modèle peut déformer un terme : voir la section « État actuel » du [chapitre 11](11_deploiement_azure.md).
- **Journaux non corrélés.** Les lignes de l'audit portent un identifiant de requête, mais pas les lignes internes (routage, appels au LLM). Avec plusieurs utilisateurs, seule la ligne d'audit relie une question à son résultat.

## 12.5 Le plan

| Lot | Contenu | Coût Azure en plus | État |
|---|---|---|---|
| **1. Exploitation** | File d'attente, audit, mise à jour sans coupure, délais maximum | Quasi nul | **Fait** |
| **2. Identité** | Connexion par compte Microsoft, rôles, audit nominatif, documents isolés par personne | Aucun | Déployé ; à vérifier avec deux vraies personnes |
| **3. Qualité** | Modèle CPU choisi par mesure, contrôle des réponses, évaluation en CI, retours utilisateurs | Faible | Écarté le 04/11/2026 : la priorité est la partie production |
| **4. Livraison** | Alertes et retour arrière (faits), test de restauration, déploiement automatique, préproduction | Quasi nul | En cours |
| **5. Sécurité** | Key Vault, identité managée, réseau privé | Faible | À faire |

**Hors de portée avec le crédit étudiant :** une réplique toujours active, plusieurs répliques et une base en haute disponibilité. Au tarif actif de 0,60 $ de l'heure, une seule réplique sollicitée en permanence coûterait environ 440 $ par mois, soit plus que le crédit total. Le tarif au repos est plus bas, mais je ne l'ai pas chiffré.

## 12.6 Comment fonctionne le lot 1

- **File d'attente** ([app/admission.py](../app/admission.py)). Le LLM sur CPU traite efficacement une demande à la fois. Les suivantes attendent leur tour ; l'ingestion passe par la même file, car elle sollicite aussi le CPU. Réglages : `MAX_ACTIVE_REQUESTS`, `REQUEST_QUEUE_SIZE`, `REQUEST_WAIT_SECONDS`.
- **Audit** ([app/audit.py](../app/audit.py)). Chaque question, dépôt et suppression est enregistré dans la table `audit_log`, consultable avec `GET /api/audit`. Le rôle lecture seule de l'outil SQL n'y a pas accès.
- **Sondes.** `/api/live` dit si le processus répond, `/api/ready` dit si les modèles sont chargés. Azure n'envoie du trafic à une nouvelle version qu'une fois celle-ci prête.
- **Arrêt progressif.** Lors d'une mise à jour, l'ancienne version a 120 s pour finir ses réponses. Ollama ignore le signal d'arrêt pendant ce délai, sinon il couperait les réponses en cours.

## 12.7 En entretien

- *« Comment isoles-tu les données des utilisateurs ? »* Dans la base, pas seulement dans le code : Row-Level Security pour les passages, un schéma et un rôle par personne pour les tables. Le SQL écrit par le LLM s'exécute sous le rôle de la personne, donc même une injection de prompt ne peut pas lire les tables d'un autre. Et je le prouve à chaque push : des tests attaquent un vrai PostgreSQL.

- *« Ce système tiendrait-il en production ? »* Pas à fort trafic : il traite une question à la fois sur CPU. Il a en revanche ce que la production exige : file d'attente, audit, mise à jour sans coupure. J'ai mesuré 10 demandes simultanées, et je sais ce qui se passe au-delà : un refus explicite, pas une panne.
- *« Pourquoi une file plutôt que traiter en parallèle ? »* Sur 2 vCPU, des générations simultanées se partageraient le même CPU : toutes ralentiraient, et elles risqueraient d'atteindre ensemble la limite de 240 s de l'entrée.
- *« Que changerais-tu avec du budget ? »* Un LLM managé ou un GPU, plusieurs répliques avec la recherche lexicale dans Postgres, et une base en haute disponibilité. Le code et le pipeline de livraison restent les mêmes.
