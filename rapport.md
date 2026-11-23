# Rapport — De la facture scannée à l'agent IA

> Projet DAMS5 · Hyperfluid · 2026

## 1. Architecture générale

```
 PDF (bucket S3 "factures")
        │
        ▼
 Détection texte / image ──► PDF natif : extraction directe de la couche texte
        │                └─► PDF scanné : rendu en image ──► LightOnOCR-3 (API Hyperfluid)
        ▼
 BRONZE  (texte brut + métadonnées de traçabilité, Parquet sur S3)
        │
        ▼
 SILVER  (tables typées : factures, lignes_facture, quarantaine — Parquet sur S3)
        │
        ▼
 Entrepôt DuckDB (vues SQL sur le Parquet S3 : stockage ≠ calcul)
        │
        ▼
 Serveur MCP (outils en lecture seule) ──► Agent IA (LLM interchangeable)
```

### Choix techniques

| Brique | Choix | Justification |
|---|---|---|
| Langage | Python 3.12 | Écosystème data/IA le plus riche (S3, PDF, Parquet, DuckDB, MCP, SDK des LLM). |
| Gestion du projet | `uv` | Un seul outil pour installer Python, les dépendances (verrouillées dans `uv.lock`) et lancer les commandes : `uv run factures …`. Un nouvel arrivant n'a rien d'autre à installer. |
| Accès S3 | `boto3` | Client S3 de référence ; fonctionne avec toute passerelle compatible S3 (Hyperfluid, MinIO, AWS) en changeant seulement l'URL. |
| Lecture des PDF | `PyMuPDF` | Lit la couche texte d'un PDF **et** sait convertir une page en image (nécessaire pour l'OCR à l'étape 2) : une seule bibliothèque pour les deux besoins. |
| Appel de l'OCR | SDK `openai` | Le modèle LightOnOCR-3 est servi derrière une API compatible OpenAI. L'adresse, la clé et le nom du modèle sont lus dans le `.env` : pour changer de fournisseur d'OCR, il suffit de modifier ces 3 variables, sans toucher au code. |
| Format des couches bronze et silver | Parquet (`pyarrow`) | Format en colonnes, compressé et **typé** (dates, décimaux), lisible directement par les moteurs SQL (DuckDB, Spark…). C'est le format standard d'un data lake. |
| Moteur SQL (entrepôt) | DuckDB + extension `httpfs` | Moteur SQL analytique embarqué : pas de serveur à installer ni à administrer. Il lit directement les fichiers Parquet sur S3, ce qui permet de séparer stockage et calcul. Il est disponible en bibliothèque Python et comme pilote dans DBeaver. |
| Serveur MCP | SDK officiel `mcp` (Python, classe `MCPServer`) | Bibliothèque de référence du protocole. Les outils sont de simples fonctions Python : leur nom, leur docstring et leurs paramètres typés deviennent automatiquement la description que lit le LLM. Transport `stdio` : le client lance le serveur comme un sous-processus, sans port réseau ouvert. |
| Agent | boucle d'agent écrite à la main + SDK `openai` + client MCP | Pas de framework d'agent : la boucle « le LLM demande un outil → on l'exécute via MCP → on lui renvoie le résultat » tient en une cinquantaine de lignes, et on contrôle exactement ce qui est affiché. Le protocole « OpenAI chat completions » est parlé par presque tous les fournisseurs (Ollama, vLLM, Hyperfluid, Mistral, OpenAI…) : changer de LLM revient à changer 3 variables. |
| Conteneur | Docker (`python:3.12-slim` + `uv`) | Exécution identique sur tous les postes et sur la plateforme ; secrets fournis au lancement, jamais dans l'image. |
| Secrets | fichier `.env` ignoré par Git | Les identifiants S3 et la clé d'API OCR ne sont jamais dans le dépôt ; seul `.env.example` (sans valeurs) est versionné. |
| Droits d'accès | une clé par bucket, droits minimum | Sur Hyperfluid une clé S3 est confinée à un bucket, et la clé du bucket source est en lecture seule. Principe du **moindre privilège** : une clé qui fuit ne permet pas de détruire des données. Pour l'OCR, le scope `model:inference` a été refusé par la plateforme (voir partie 5), d'où l'utilisation de `model:*`. |

## 2. Déroulé du projet, étape par étape

### Étape 0 — Mise en place du projet

- Création du dépôt Git et d'un `.gitignore` qui exclut les secrets (`.env`), les données locales (`data/`) et les fichiers DuckDB.
- Les paramètres de configuration sont dans un fichier `.env.example`, versionné mais sans valeurs : chaque développeur le copie en `.env` et y met ses propres identifiants. **Pourquoi ?** Une clé committée reste dans l'historique Git même après suppression ; il faudrait alors la révoquer.

### Étape 1 — Récupérer les factures

**Commandes**

```bash
uv run factures fetch    # télécharge les nouvelles factures dans data/landing/
uv run factures detect   # classe chaque PDF : texte ou image
```

**Récupération incrémentale (`fetch`).** Le script liste le bucket `factures` (avec pagination, pour fonctionner au-delà de 1 000 objets) et télécharge les PDF dans une zone d'arrivée locale `data/landing/`. Un manifeste `_manifest.json` garde pour chaque fichier son **ETag** (empreinte calculée par S3), sa taille, son URI S3 d'origine et la date de téléchargement. Un fichier n'est retéléchargé que s'il est nouveau ou si son ETag a changé : un fichier modifié mais qui garde le même nom est donc bien détecté.

Résultat mesuré : 1ᵉʳ passage, 100 PDF téléchargés ; 2ᵉ passage, 0 téléchargé et 100 déjà présents.

**Configuration multi-clés.** Une clé S3 Hyperfluid n'ouvre qu'un seul bucket. Les variables d'environnement sont donc préfixées par bucket (`SOURCE_AWS_ACCESS_KEY_ID`, `BRONZE_AWS_…`, `SILVER_AWS_…`), et le code construit un client S3 par bucket à partir de ce préfixe (`S3Target.from_env("SOURCE")`).

**Détection texte / image (`detect`).** PyMuPDF extrait la couche texte du PDF et on compte les caractères alphanumériques. En dessous de 30, le PDF est classé `image` (OCR nécessaire), sinon `texte`. Le résultat est écrit dans `data/landing/detection.csv`.

| Type | Nombre | Caractères |
|---|---|---|
| texte | 50 | 552 à 718 |
| image | 50 | 0 |

L'écart entre les deux groupes (0 contre plus de 550 caractères) est net : le seuil de 30 ne crée aucune ambiguïté.

**Vérification.** Les noms de fichiers contiennent `_texte` ou `_scan`. Le détecteur ne s'en sert **pas**, car un nom de fichier n'est pas une donnée fiable en production. On les a seulement utilisés pour contrôler le résultat : **0 désaccord sur 100 fichiers**.

### Étape 2 — Appeler le modèle d'OCR

**Commande**

```bash
uv run factures ocr facture_A-2025-0003_scan.pdf            # image de 1540 px (par défaut)
uv run factures ocr facture_A-2025-0003_scan.pdf --taille 768
```

La commande affiche le texte reconnu, puis le temps de traitement de la page.

**Format de requête (d'après la fiche du modèle).** Toutes les recommandations de la *model card* sont appliquées dans `ocr.py` :

- la page PDF est convertie en **PNG** avec PyMuPDF, **côté le plus long à 1540 px**, en gardant les proportions pour ne pas déformer le texte ;
- l'image est envoyée **seule, en base64, sans texte d'instruction**. La fiche précise que le modèle est entraîné ainsi et que d'autres instructions sortent de son domaine d'entraînement ;
- **une seule image par requête** ;
- paramètres `temperature=0.2`, `top_p=0.9`, `max_tokens=4096`. La température basse limite les « inventions » du modèle.

**Format de sortie.** Le modèle renvoie la page en **Markdown** :

- les champs simples sont des lignes `**Libellé** : valeur` (`**Numéro de facture** : A-2025-0003`, `**Date d'émission** : 29/04/2025`…) ;
- les blocs sont séparés par des `---` ;
- le **tableau des lignes de facture est rendu en HTML** (`<table>`, `<thead>`, `<tr>`, `<td>`), pas en tableau Markdown ;
- les montants gardent le format français (`7 567,26 €` : espace pour les milliers, virgule décimale, symbole €).

Conséquence pour l'étape 4 : le texte OCR n'a pas la même forme que la couche texte d'un PDF natif, et la mise en forme varie d'un appel à l'autre. Par exemple, `**Numéro de facture** :` à 1540 px devient `Numéro de facture :` sans gras à 768 px. Le parseur devra ignorer le gras et savoir lire un tableau HTML.

**Temps de traitement par page** (factures scannées, une page chacune) :

| Taille de l'image | Mesures | Temps moyen par page |
|---|---|---|
| 1540 px (recommandé) | 4 factures : 7,30 · 7,91 · 8,42 · 8,69 s | **≈ 8,1 s** |
| 768 px | 1 facture | 6,1 s |
| 2500 px | 1 facture | 9,7 s |

À 8 s par page, les 50 factures scannées représentent environ 7 minutes d'OCR. C'est pour cette raison que le pipeline devra être incrémental (étape 3) et ne jamais ré-OCRiser un fichier déjà traité.

**Compromis taille / vitesse / qualité.** Une image plus petite est plus rapide (moins de pixels à analyser), mais les caractères deviennent flous et le risque d'erreur augmente sur les chiffres. Une image plus grande est plus lente sans apporter de gain au-delà de la résolution d'entraînement du modèle. Sur la facture testée, les trois tailles donnent le même numéro, le même SIRET et le même total TTC. La mesure rigoureuse de la qualité sera faite à l'étape 5, sur un échantillon.

**Indépendance vis-à-vis du fournisseur.** Le code n'utilise que le protocole standard « OpenAI chat completions ». Changer de moteur d'OCR (autre modèle servi par vLLM, autre plateforme) revient à modifier `OCR_ENDPOINT`, `OCR_API_KEY` et `OCR_MODEL` dans le `.env`.

**Stockage de la clé.** La clé est uniquement dans le `.env`, exclu par le `.gitignore`. Le dépôt ne contient que `.env.example`, sans valeurs.

### Étape 3 — Couche bronze

**Commande**

```bash
uv run factures bronze
```

**Stockage.** La couche bronze est un ensemble de fichiers **Parquet** dans le bucket S3 `default-bronze`, sous `documents/`. Chaque exécution du pipeline écrit un **nouveau** fichier `lot_<date>.parquet` qui contient uniquement les documents traités pendant ce lancement.

**Contenu d'une ligne (un document)**

| Colonne | Rôle |
|---|---|
| `sha256` | identifiant du document : empreinte de son contenu |
| `fichier`, `s3_uri`, `s3_etag`, `taille_octets` | d'où vient le document (traçabilité vers le bucket source) |
| `type_pdf` | « texte » ou « image » (étape 1) |
| `methode`, `modele_ocr`, `taille_image_px` | comment le texte a été obtenu (couche texte ou OCR, avec quel modèle et quelle résolution) |
| `texte` | texte brut, **tel qu'extrait**, sans aucune correction |
| `duree_s` | temps d'extraction |
| `statut`, `erreur` | `ok`, ou `erreur` avec le message |
| `ingere_le` | date et heure d'ingestion |

Grâce à ces métadonnées, on pourra expliquer plus tard d'où vient une valeur de la couche silver : de quel fichier, à quel emplacement S3, extraite par couche texte ou par OCR, avec quel modèle et à quelle date.

**Reconnaître un fichier « déjà vu ».** Le nom ne suffit pas : deux factures différentes peuvent porter le même nom, et une même facture peut être renommée ou déposée deux fois. On utilise donc l'**empreinte SHA-256 du contenu**. Au lancement, le pipeline lit les empreintes déjà présentes en bronze avec `statut = ok` et ne traite que les autres.

**Gestion des erreurs.** L'extraction est entourée d'un `try/except`. Si l'OCR ne répond pas, le document est enregistré avec `statut = erreur` et le message, puis le pipeline passe au suivant. Au lancement suivant, comme ce document n'a pas de ligne `ok`, il est automatiquement retenté. Le client OCR a un délai maximum de 120 s et fait 2 nouvelles tentatives, pour qu'une panne ne bloque pas le pipeline indéfiniment.

**Pourquoi ne jamais modifier ni supprimer le bronze ?** C'est la seule copie fidèle de ce qui a été extrait à un instant donné. Si le parseur silver a un bug, ou si on améliore les règles de qualité, on peut **reconstruire entièrement la couche silver** à partir du bronze, **sans refaire l'OCR** (lent : environ 8 s par page, et payant). Les erreurs restent aussi visibles dans l'historique. En pratique, le bronze est en ajout seul (nouveaux fichiers uniquement), et la clé S3 du bucket bronze **n'a pas le droit de supprimer** : nous l'avons vérifié, un `DeleteObject` renvoie `AccessDenied`. La plateforme garantit donc elle-même cette règle.

**Tests (critères de validation)**

| Test | Résultat |
|---|---|
| 1. OCR volontairement indisponible (`OCR_ENDPOINT` vers une adresse qui ne répond pas) | Pas de plantage : 50 PDF texte ingérés (`ok`), 50 scans enregistrés en `erreur` avec le motif `APIConnectionError: Connection error.` |
| 2. Lancement normal | Seuls les 50 scans en erreur sont retraités, avec succès : 50 ingérés, 0 erreur, 50 déjà ingérés. Durée : 6 min 30, soit en moyenne **7,1 s par page OCR** (min 5,6 s, max 10,0 s). |
| 3. Relancement immédiat | **0 ingéré, 100 déjà ingérés, aucun appel OCR**, en 3,5 s. |

Après ces 3 lancements, la couche bronze contient **150 lignes** : 50 `couche_texte/ok`, 50 `erreur` (le test 1, conservées comme historique) et 50 `ocr/ok`. Cela fait **100 documents distincts avec `statut = ok`**, aucune facture n'est perdue. Les lignes en erreur ne sont pas supprimées, car le bronze n'est jamais modifié. La couche silver ne prendra que la ligne `ok` de chaque document.

### Étape 4 — Couche silver

**Commande**

```bash
uv run factures silver
```

**Fonctionnement.** La couche silver est **entièrement recalculée à partir du bronze** à chaque lancement, en prenant la dernière extraction réussie de chaque document. Aucun appel OCR n'est nécessaire : toute la chaîne prend quelques secondes. Si on corrige le parseur ou une règle de qualité, il suffit de relancer la commande. Trois tables Parquet sont écrites dans le bucket `default-silver` :

| Table | Contenu | Lignes |
|---|---|---|
| `factures` | une ligne par facture acceptée : fournisseur, client, SIRET, dates, catégorie, statut de paiement, totaux, **statut de qualité**, anomalies, et lien vers le bronze (`sha256`, `fichier`, `methode_extraction`) | 100 |
| `lignes_facture` | une ligne par article : désignation, quantité, prix unitaire HT, taux de TVA, total HT | 271 |
| `quarantaine` | documents rejetés : fichier, **motif**, détail, date du rejet | 0 |

**Typage.** Les dates sont en `date32` et les quantités en entiers. Les **montants sont en `DECIMAL(12,2)`** dans le Parquet et en `Decimal` dans le code Python, **jamais en nombre flottant**. Un `float` ne sait pas représenter exactement la plupart des centimes : `0.1 + 0.2` donne `0.30000000000000004`. Sur des sommes de factures, ces erreurs s'accumulent et faussent les contrôles d'égalité (HT + TVA = TTC). Le type décimal est exact au centime.

**Un parseur robuste aux deux formes de texte.** Le texte natif a une valeur par ligne. Le texte OCR est en Markdown (gras `**` présent ou non, titres `#`, séparateurs `---`), avec des tableaux HTML et des totaux tantôt en texte, tantôt dans un second tableau HTML. Plutôt que d'écrire deux parseurs, on **normalise** d'abord le texte : on retire la mise en forme Markdown, on supprime les balises HTML et on met chaque cellule de tableau sur sa propre ligne. Les deux sources ont alors la même forme, et **un seul parseur** les lit. Les champs sont recherchés par leur libellé (`Numéro de facture :`, `Statut :`…) et non par leur position. Un montant n'est accepté que s'il respecte strictement le format français (`1 541,05 €`). Sinon, il est considéré comme illisible plutôt que deviné. Résultat : **tous les champs sont lus pour les 100 factures**, natives comme OCR.

**Contrôles de qualité.** Deux niveaux :

| Niveau | Règles | Conséquence |
|---|---|---|
| **Rejet** (document inexploitable) | champ obligatoire illisible (numéro, fournisseur, client, date d'émission, totaux) ; aucune ligne de facture ; ligne d'article illisible ; **doublon** (numéro de facture déjà accepté) | table `quarantaine`, avec motif et détail |
| **Signalement** (facture exploitable mais suspecte) | somme des lignes ≠ total HT ; HT + TVA ≠ TTC ; TVA ≠ HT × taux ; quantité × prix unitaire ≠ total de ligne ; échéance avant émission ; SIRET qui n'a pas 14 chiffres | gardée dans `factures` avec `statut_qualite = "signalee"` et la liste des anomalies |

Une tolérance d'un centime est appliquée aux comparaisons de montants, pour absorber les arrondis.

**Rejetée ou signalée ?** Une facture *rejetée* ne peut pas être utilisée : il manque une information indispensable, ou c'est un doublon qui fausserait les totaux. Elle est écartée de la table des factures et le motif est conservé pour qu'un humain puisse la traiter. Une facture *signalée* a toutes ses informations, mais elles se contredisent. Elle reste dans les données, parce que la supprimer ferait « perdre » une facture réelle, mais elle porte un statut qui permet de l'exclure des calculs ou de la faire vérifier.

**Faut-il corriger automatiquement un montant mal lu ?** Non. Si HT + TVA ≠ TTC, on ne sait pas quel montant est faux : le HT, la TVA ou le TTC. Une « correction » automatique choisirait au hasard, et le chiffre serait faux sans que personne ne le sache. Pire, l'erreur pourrait venir de la facture elle-même (erreur ou fraude du fournisseur), et la corriger la masquerait. On **signale** donc sans modifier : la valeur reste celle lue, l'anomalie est visible, et la décision revient à un humain. La valeur d'origine reste de toute façon disponible dans le bronze.

**Résultats**

- 100 factures : **99 valides, 1 signalée, 0 rejetée** ; 271 lignes de facture.
- Facture signalée : **V-2025-0001**, avec Total HT 148,26 € + TVA 29,65 € mais **Total TTC 99 999,99 €**. En vérifiant l'image du scan, on voit que ce montant est bien imprimé sur la facture : l'OCR l'a lu correctement. C'est une anomalie volontaire du jeu de données, et le contrôle « HT + TVA ≠ TTC » la détecte.
- Aucun doublon dans le jeu fourni. Pour vérifier les règles de rejet, nous avons simulé en mémoire deux documents supplémentaires. Une copie renommée d'une facture existante (contenu identique, autre nom) part en quarantaine avec le motif `doublon` (*numéro déjà présent dans facture_A-2025-0001_texte.pdf*). Un texte OCR où « Total TTC » est mal lu (« Tota1 TTC ») part en quarantaine avec le motif `champ_obligatoire_illisible` (*total_ttc*). Les 100 vraies factures restent acceptées.

**Critères de validation**

- *Aucune facture valide n'est perdue* : 100 documents dans le bronze, donc 100 factures dans le silver.
- *Aucun doublon ne passe* : contrôle sur le numéro de facture, en plus de l'empreinte SHA-256 du bronze. Vérifié par simulation.
- *Chaque facture porte un statut de qualité, chaque rejet un motif* : colonnes `statut_qualite` et `quarantaine.motif`.
- *Les montants incohérents sont détectés* : V-2025-0001 est signalée.

### Étape 5 — Mesure de la qualité de l'OCR

**Commandes**

```bash
uv run factures evaluer                # compare la couche silver (OCR à 1540 px) à la vérité terrain
uv run factures evaluer --taille 768   # ré-OCRise l'échantillon à 768 px et compare
```

Chaque commande affiche le tableau de précision par champ, la liste des erreurs (valeur attendue / valeur lue), et écrit le résultat dans `evaluation/resultats_<réglage>.csv`. Le texte OCR des réglages testés est mis en cache dans `data/evaluation/` : relancer la commande ne refait pas l'OCR.

**Échantillon.** 20 factures scannées sur 50 (40 %), tirées au hasard avec une graine fixe (`random.Random(42)`) pour que le tirage soit reproductible. Seules les factures **scannées** sont évaluées : pour les PDF natifs, le texte vient directement de la couche texte, sans OCR.

**Constituer une vérité terrain fiable sans y passer des heures.** Ressaisir 20 factures × 16 champs à la main serait long, avec un risque d'erreurs de frappe sur les SIRET et les IBAN. Nous avons procédé en trois temps :

1. **Pré-remplissage** du fichier `evaluation/verite_terrain.csv` avec les valeurs de la couche silver.
2. **Vérification visuelle** de chaque champ sur l'image du scan, facture par facture. Le risque de cette méthode est le *biais de confirmation* : on a tendance à valider ce qui est déjà écrit. C'est pourquoi on la complète par l'étape suivante.
3. **Contrôle croisé automatique** des identifiants longs (SIRET fournisseur, TVA intracom., IBAN, SIRET client) avec les **PDF natifs** des mêmes fournisseurs et clients. Leur texte est exact, puisqu'il est lu sans OCR. Résultat : **40 contrôles sur 40 identiques**.

Aucune correction n'a été nécessaire : la vérité terrain est identique aux valeurs lues à 1540 px.

**Quelle taille d'échantillon ?** Avec 20 factures, une erreur fait varier la précision d'un champ de 5 points : la mesure est grossière, mais suffisante pour **comparer deux réglages**. Quand on n'observe aucune erreur sur *n* essais, la « règle de trois » dit que le vrai taux d'erreur est, avec 95 % de confiance, inférieur à environ 3/*n*. Ici, cela donne moins de 15 % par champ (3/20) et moins de 1 % tous champs confondus (3/320). Pour affiner la mesure d'un champ précis, il faudrait plus de factures, idéalement les 50 scans.

**Résultats** : voir la partie 3.

### Étape 6 — Entrepôt de données

**Commandes**

```bash
uv run factures sql "SELECT statut_paiement, count(*), sum(total_ttc) FROM silver.factures GROUP BY 1"
uv run factures dbeaver   # génère data/dbeaver_init.sql pour l'outil graphique
```

**Séparation stockage / calcul.**

- **Stockage** : les fichiers Parquet des buckets S3 `default-bronze` et `default-silver`. C'est le **seul exemplaire** des données.
- **Calcul** : DuckDB, un moteur SQL qui lit ces fichiers au moment de la requête.

L'entrepôt ne contient **que des vues**, c'est-à-dire des requêtes enregistrées. Aucune donnée n'est copiée dans une base :

| Vue | Fichiers lus |
|---|---|
| `bronze.documents` | `s3://default-bronze/documents/*.parquet` |
| `silver.factures` | `s3://default-silver/factures/*.parquet` |
| `silver.lignes_facture` | `s3://default-silver/lignes_facture/*.parquet` |
| `silver.quarantaine` | `s3://default-silver/quarantaine/*.parquet` |

Chaque programme ouvre sa **propre connexion DuckDB en mémoire** (`entrepot.connexion()`) : il charge l'extension `httpfs`, déclare une clé S3 par bucket (limitée à ce bucket grâce à `SCOPE`) et crée les vues. Le calcul peut ainsi être lancé n'importe où (poste de travail, conteneur, plusieurs programmes en parallèle), sans déplacer ni dupliquer les données.

**Depuis les programmes** : `from factures.entrepot import connexion; connexion().sql("SELECT …")`, ou la commande `uv run factures sql "…"`. Le serveur MCP de l'étape 7 utilisera cette même fonction.

**Depuis DBeaver** : `uv run factures dbeaver` génère `data/dbeaver_init.sql`, le même script d'initialisation que celui des programmes. Dans DBeaver :

1. *Nouvelle connexion* → **DuckDB** → chemin `:memory:`.
2. Ouvrir un éditeur SQL sur cette connexion, coller le contenu du script et l'exécuter (*Exécuter le script*, Alt+X).
3. Les schémas `bronze` et `silver` apparaissent : on peut alors interroger les tables.

*Test réalisé* : DBeaver Community (Windows) avec une connexion DuckDB `:memory:`. La requête `SELECT numero_facture, client, total_ttc, statut_qualite FROM silver.factures` affiche les 100 factures. DBeaver est sous Windows et le projet dans WSL, et cela fonctionne quand même, puisque les deux lisent les mêmes fichiers sur S3. Remarque : DBeaver affiche `36973,8` au lieu de `36 973,80`, ce n'est qu'un format d'affichage, la colonne est bien un `DECIMAL(12,2)` exact.

![alt text](image-2.png)

Pour ne pas avoir à le refaire à chaque ouverture, on peut coller le script dans *Connexion → Initialisation → Requêtes de démarrage (bootstrap queries)*. Ce script contient les clés S3 : il est généré dans `data/`, ignoré par Git, et n'est jamais versionné.

**Que se passe-t-il si plusieurs programmes accèdent aux mêmes données en même temps ?** Un fichier de base DuckDB (`.duckdb`) ne peut être ouvert en écriture que par **un seul processus** à la fois. Nous l'avons vérifié : quand un programme a ouvert le fichier, un second programme qui veut l'ouvrir, même en lecture seule, reçoit `IO Error: Could not set lock on file`. Si DBeaver gardait un tel fichier ouvert, le pipeline et le serveur MCP seraient bloqués. C'est pour cela que nous n'utilisons **pas de fichier partagé** : chaque client a son moteur en mémoire, et le seul élément partagé, le stockage S3, accepte un nombre quelconque de lecteurs simultanés. Côté écriture, la couche silver est reconstruite en remplaçant chaque fichier Parquet par un `PutObject` S3, qui est **atomique** : un lecteur voit soit l'ancienne version complète, soit la nouvelle, jamais un fichier à moitié écrit.

*Test réalisé* : 3 requêtes SQL lancées en même temps qu'une reconstruction complète de la couche silver. Les 3 lecteurs obtiennent le bon résultat (100 factures, 972 687,52 € TTC) et la reconstruction se termine normalement, sans blocage.

**Comment les références aux fichiers restent-elles valides partout ?** Les vues ne contiennent **aucun chemin local** (`/home/…` ou `C:\…`) : elles pointent vers des adresses **`s3://bucket/…`**, identiques sur ma machine, dans un conteneur ou chez un collègue. Ce qui change d'un environnement à l'autre, ce sont les identifiants et l'adresse du service S3. Ils sont lus dans le `.env` (ou dans les variables d'environnement d'un conteneur) au moment de la connexion. Les noms des buckets viennent aussi de la configuration : changer de bucket ne demande aucune modification du code.

**Exemple de requête** (montant TTC par statut de paiement) :

| statut_paiement | nb | total_ttc |
|---|---|---|
| En attente | 21 | 129 064,97 € |
| En retard | 21 | 211 938,66 € |
| Payée | 58 | 631 683,89 € |

### Étape 7 — Serveur MCP

**Commandes**

```bash
uv run factures-mcp              # démarre le serveur (normalement lancé par le client MCP)
uv run python tests/test_mcp.py  # script de test de tous les outils
```

**Outils exposés** (`src/factures/mcp_server.py`)

| Outil | Rôle | Paramètres |
|---|---|---|
| `lister_factures` | liste des factures | fournisseur, client (recherche partielle), statut_paiement, statut_qualite, categorie, date_debut, date_fin, limite |
| `totaux_factures` | nombre et sommes HT / TVA / TTC | mêmes filtres |
| `detail_facture` | une facture et ses lignes d'articles | numero_facture |
| `schema_donnees` | tables, colonnes et types | — |
| `requete_sql` | requête SQL libre en lecture seule (200 lignes max) | sql |

**Transparence.** Chaque réponse contient la clé `sql`, avec la ou les requêtes réellement exécutées, et la clé `parametres`, avec les valeurs des filtres. Par exemple, `detail_facture` renvoie ses deux requêtes (en-tête et lignes). L'utilisateur peut ainsi vérifier d'où vient chaque chiffre, ou relancer la requête lui-même dans DBeaver.

**Bien rédiger noms et descriptions.** Le LLM choisit ses outils uniquement d'après leur nom, leur description et leurs paramètres. Nous avons donc :

- des **noms explicites** en verbe + objet (`totaux_factures` plutôt que `stats`) ;
- une description qui dit **quand** utiliser l'outil, et quand en préférer un autre. Par exemple : « pour toute question *combien*, *quel montant*, utiliser `totaux_factures` », plutôt que de laisser le LLM additionner lui-même une liste, ce qui est source d'erreurs ;
- les **valeurs possibles** des filtres (« Payée », « En retard », « En attente » ; les 4 catégories) et le **format attendu** (dates `AAAA-MM-JJ`, avec l'exemple de l'année 2025) ;
- `requete_sql` présenté comme un **dernier recours**, avec la consigne d'appeler `schema_donnees` d'abord.

**Éviter les injections SQL.** Les arguments viennent d'un LLM, donc d'une source non fiable : un texte malveillant dans une facture pourrait par exemple pousser le LLM à envoyer `x'; DROP …`. Nous appliquons trois protections :

1. **Requêtes paramétrées** dans les outils à filtres : les valeurs sont transmises à DuckDB à part (`$1`, `$2`…) et ne sont **jamais** collées dans le texte SQL. Elles ne peuvent donc pas en changer la structure. Seule la `limite` est insérée dans le texte, après avoir été validée comme entier et bornée entre 1 et 200.
2. **Analyse de la requête libre par DuckDB lui-même** (`extract_statements`) : on exige **une seule instruction**, de type **SELECT**. Tout le reste (DELETE, DROP, CREATE, COPY, ATTACH, SET, plusieurs instructions…) est refusé avant exécution. On utilise l'analyseur SQL du moteur plutôt qu'une recherche de mots-clés, qui serait facile à contourner.
3. **Verrouillage du moteur** au démarrage : `enable_external_access = false` interdit toute lecture de fichiers locaux ou d'autres URL, `allowed_directories` n'autorise que nos deux buckets, et `lock_configuration = true` empêche de modifier ces réglages par une requête.

Pourquoi la protection 2 est indispensable : lors de nos essais, le verrouillage seul (protection 3) laissait passer un `COPY (SELECT 1) TO 's3://default-silver/…'`, qui **a réellement écrit un fichier** dans le bucket silver. La clé S3 du silver a en effet les droits d'écriture, nécessaires au pipeline. Le fichier a été supprimé. Une amélioration serait de donner au serveur MCP **ses propres clés S3 en lecture seule**. L'écriture serait alors refusée par la plateforme elle-même, comme la suppression sur le bucket bronze.

**Que renvoyer en cas d'erreur ?** Jamais une exception brute, mais un objet `{"erreur": …, "sql": […], "conseil": …}` : le message d'erreur de DuckDB, la requête fautive et une piste de correction (« Vérifiez les noms de colonnes avec schema_donnees », « dates en AAAA-MM-JJ », « utilisez lister_factures pour retrouver le numéro exact »). Le LLM peut ainsi corriger son appel et réessayer, au lieu d'abandonner ou d'inventer une réponse.

**Script de test** (`tests/test_mcp.py`). Il démarre le serveur comme un vrai client MCP (transport stdio) et appelle chaque outil. Il compare les résultats à des requêtes SQL faites **directement** sur l'entrepôt, de façon indépendante du serveur, puis tente 9 modifications malveillantes. Il vérifie enfin que les données et le bucket silver sont intacts. Résultat : **27/27 vérifications réussies**.

| Groupe | Vérifications |
|---|---|
| Outils exposés | les 5 outils sont présents |
| `lister_factures` | filtre fournisseur + statut ; SQL renvoyé ; valeurs passées en paramètres (absentes du texte SQL) ; filtre période |
| `totaux_factures` | nombre et total TTC exacts (en retard, 2025) ; filtre sans résultat → 0 |
| `detail_facture` | en-tête ; nombre de lignes ; 2 requêtes renvoyées ; numéro inconnu → erreur avec conseil |
| `schema_donnees` | 4 tables ; colonnes typées |
| `requete_sql` | SELECT accepté ; colonne inexistante → erreur avec conseil |
| Tentatives refusées | DELETE, DROP VIEW, CREATE TABLE, COPY vers S3, 2 instructions, ATTACH, SET, lecture du `.env`, lecture des secrets |
| Intégrité | 100 factures après les attaques ; aucun fichier ajouté ou supprimé dans le bucket silver |

**Branchement sur un client MCP.** Le fichier `.mcp.json` à la racine du projet déclare le serveur pour **Claude Code** (`uv run factures-mcp`). À l'ouverture du projet, Claude Code propose de l'activer, et les 5 outils deviennent disponibles dans la conversation.

*Conversation de démonstration* : à la question « Quel est le montant TTC des factures en retard en 2025 ? », la réponse attendue est **12 factures, 106 886,43 € TTC** (vérifiée par SQL : `SELECT count(*), sum(total_ttc) FROM silver.factures WHERE statut_paiement = 'En retard' AND year(date_emission) = 2025`).

*Résultat obtenu dans Claude Code* : Claude appelle le serveur `factures` et répond « Les factures en retard émises en 2025 font **106 886,43 € TTC** au total. Elles sont au nombre de **12**, pour 89 072,02 € HT et 17 814,41 € de TVA ». Les chiffres sont **exacts** : ils correspondent à la requête de vérification, et 89 072,02 + 17 814,41 = 106 886,43. Claude précise aussi de lui-même son interprétation : « 2025 » est pris sur la date d'émission, et le total inclut les factures `signalee`. C'est grâce aux descriptions des outils, qui documentent les filtres et les statuts de qualité.

![Conversation dans Claude Code utilisant le serveur MCP factures](image-1.png)

Détail de l'appel d'outil, affiché dans Claude Code (Ctrl+O) :

```
● factures - totaux_factures (MCP)(statut_paiement: "En retard", date_debut: "2025-01-01", date_fin: "2025-12-31")
{
  "nombre_factures": 12,
  "total_ht": "89072.02",
  "total_tva": "17814.41",
  "total_ttc": "106886.43",
  "sql": [
    "SELECT count(*) AS nombre_factures, coalesce(sum(total_ht), 0) AS total_ht, coalesce(sum(total_tva), 0) AS total_tva, coalesce(sum(total_ttc), 0) AS total_ttc FROM silver.factures WHERE statut_paiement = $1 AND date_emission >= CAST($2 AS DATE) AND date_emission <= CAST($3 AS DATE)"
  ],
  "parametres": ["En retard", "2025-01-01", "2025-12-31"]
}
```

Cet extrait montre les points demandés :

- **choix de l'outil** : pour une question « quel montant », le LLM choisit bien `totaux_factures` et non `lister_factures`, comme l'indique sa description ;
- **arguments** : il traduit « en retard » par la valeur exacte `"En retard"`, et « en 2025 » par une période au format `AAAA-MM-JJ`, tous deux documentés dans la description ;
- **transparence** : la requête SQL réellement exécutée est renvoyée ;
- **sécurité** : les valeurs sont des paramètres (`$1`, `$2`, `$3`), absents du texte SQL ;
- **montants exacts** : ils sont transmis sous forme de texte décimal (`"106886.43"`).

### Étape 8 — Agent

**Commandes**

```bash
uv run factures-agent                         # conversation ; « quitter », Ctrl+D ou Ctrl+C pour arrêter
uv run factures-agent "Quel est le montant TTC des factures en retard en 2025 ?"   # une seule question
```

**Fonctionnement** (`src/factures/agent.py`)

1. L'agent démarre le serveur MCP de l'étape 7 (`factures-mcp`) et récupère la liste de ses 5 outils. Il les traduit au format « tools » de l'API OpenAI : même nom, même description, même schéma de paramètres.
2. Pour chaque question, il envoie au LLM l'historique et les outils. Si le LLM demande un outil, l'agent l'exécute **via le serveur MCP**, affiche l'appel, puis renvoie le résultat au LLM. On recommence jusqu'à obtenir une réponse en texte.
3. L'agent n'a **aucun autre accès aux données** que ces outils : pas de connexion directe à DuckDB ni à S3.

**LLM interchangeable par configuration.** Le LLM est défini par trois variables : `LLM_BASE_URL`, `LLM_API_KEY` et `LLM_MODEL`. On les met dans le `.env`, ou on les surcharge pour une seule commande. Le code utilise uniquement le protocole standard « OpenAI chat completions », que parlent Ollama (local), vLLM, la passerelle Hyperfluid, Mistral, OpenAI…

| Profil | LLM_BASE_URL | LLM_MODEL |
|---|---|---|
| Local (Ollama sous Windows, appelé depuis WSL) | `http://172.19.224.1:11434/v1` | `llama3.2:3b` |
| Hébergé (Hyperfluid AI Gateway) | `https://llm.hyperfluid.cloud/v1/organizations/<org>/shared-models/granite-4-2-8b/v1` | `granite-4-2-8b` |

**Visibilité.** Pour chaque appel, l'utilisateur voit le nom de l'outil et ses arguments (`→ outil`), la requête SQL exécutée et ses paramètres (`SQL`, `params`), puis un aperçu du résultat brut (`résultat`). Il peut ainsi contrôler chaque chiffre de la réponse.

**Fil de la conversation et arrêt propre.** L'historique (questions, appels d'outils, résultats, réponses) est conservé tout au long de la session : une question de suivi comme « Et parmi elles, lesquelles viennent d'Aubry ? » est comprise. L'agent s'arrête proprement sur `quitter`, Ctrl+D ou Ctrl+C, et le serveur MCP est fermé. Une question est limitée à 8 appels d'outils, pour éviter qu'un LLM tourne en boucle. Si le LLM est injoignable, l'erreur est affichée sans arrêter la conversation.

**Comment empêcher l'agent d'inventer des chiffres ?**

- **Consignes système** : n'utiliser que les outils, ne jamais inventer de valeur, dire clairement quand les outils ne permettent pas de répondre, et **ne pas faire les additions soi-même** mais appeler `totaux_factures`. Un LLM calcule mal, alors que SQL calcule exactement.
- **Des outils qui font le travail** : les totaux sont calculés par DuckDB sur des `DECIMAL`, et le LLM ne fait que recopier le résultat.
- **`temperature = 0`**, pour des réponses aussi stables que possible.
- **Transparence** : comme le SQL et le résultat brut sont affichés, l'utilisateur peut vérifier que le chiffre annoncé est bien celui renvoyé par l'outil.
- Limite : rien n'empêche *techniquement* un LLM de mal recopier ou d'ajouter un filtre. On le voit ci-dessous avec le petit modèle local. Le contrôle humain, rendu possible par l'affichage, reste nécessaire.

**Test : même question, plusieurs LLM, sans changer le code**

Question : « Quel est le montant TTC des factures en retard en 2025 ? ». Réponse exacte, vérifiée par SQL : **12 factures, 106 886,43 €**.

| LLM | Où | Outil et arguments | Réponse | Temps |
|---|---|---|---|---|
| `granite-4-2-8b` | hébergé (Hyperfluid, GPU) | `totaux_factures(statut_paiement="En retard", date_debut="2025-01-01", date_fin="2025-12-31")` | **106 886,43 €** ✅ | ≈ 10 s |
| `nemotron-bolt-e4b` | hébergé (Hyperfluid, GPU) | identique | **106 886,43 €** ✅ | ≈ 6 s |
| `llama3.2:3b` | local (Ollama, CPU) | `totaux_factures` avec **un filtre inventé `categorie="Informatique"`** | 70 117,42 € ❌ | ≈ 70 s |
| `llama3.2:3b`, autre essai | local | `totaux_factures` avec tous les filtres remplis par le texte `"null"` (ignorés par le serveur) | 106 886,43 € ✅ | ≈ 70 s |
| `mistral:7b` | local (Ollama, CPU) | **aucun appel d'outil** : il répond « je vais utiliser l'outil totaux_factures… » et s'arrête | pas de chiffre ❌ | 40 s à 3 min |

Le changement de LLM s'est fait uniquement par configuration, par exemple `LLM_MODEL=mistral:7b uv run factures-agent "…"`.

*Conversation de suivi* avec `granite-4-2-8b` : « Et parmi elles, lesquelles viennent du fournisseur Aubry ? ». L'agent réutilise le contexte (en retard, 2025), appelle `lister_factures(fournisseur="Aubry", statut_paiement="En retard", date_debut="2025-01-01", date_fin="2025-12-31")` et répond **A-2025-0003** (Dumas, 7 567,26 €) et **A-2025-0010** (Muller, 22 195,44 €). C'est exactement le résultat de la requête de vérification SQL.

**Observations : petit modèle local contre grand modèle hébergé**

- **Vitesse** : les modèles hébergés sur GPU répondent en 6 à 10 s. Les modèles locaux, sur le processeur de notre PC portable, prennent de 40 s à plus de 3 min.
- **Choix des outils** : les modèles hébergés choisissent toujours le bon outil, avec exactement les filtres demandés. `llama3.2:3b` choisit le bon outil, mais **remplit des paramètres de lui-même** : un filtre de catégorie inventé, ou des `"null"` en texte. Il le fait malgré la consigne explicite « n'ajoute jamais de filtre de toi-même ». `mistral:7b` (dans sa version Ollama) **n'utilise pas du tout** les outils et se contente de décrire ce qu'il ferait.
- **Exactitude** : 100 % des réponses justes pour les modèles hébergés. Pour le petit modèle local, les réponses sont **instables** : la même question donne une réponse juste ou fausse selon l'essai, malgré `temperature = 0`.
- **Enseignement** : la qualité de l'agent dépend fortement de la capacité du LLM à appeler des outils correctement. L'affichage des appels et du SQL est ce qui permet de **détecter** les erreurs, comme la mauvaise catégorie, au lieu de faire confiance à une réponse fausse mais bien formulée. Pour un usage réel, nous retenons un modèle hébergé (`granite-4-2-8b`). Le modèle local reste utile pour développer hors ligne, sans coût ni envoi de données à l'extérieur.

## 3. Résultats de l'évaluation OCR

Échantillon : 20 factures scannées, 16 champs par facture, soit 320 valeurs comparées.

| Champ | 1540 px (recommandé) | 768 px |
|---|---|---|
| numero_facture | 100 % | **95 %** |
| fournisseur | 100 % | 100 % |
| fournisseur_siret | 100 % | **95 %** |
| fournisseur_tva_intracom | 100 % | **95 %** |
| client | 100 % | 100 % |
| client_siret | 100 % | **95 %** |
| date_emission | 100 % | 100 % |
| date_echeance | 100 % | 100 % |
| categorie | 100 % | 100 % |
| statut_paiement | 100 % | 100 % |
| mode_paiement | 100 % | 100 % |
| iban | 100 % | **95 %** |
| total_ht | 100 % | 100 % |
| total_tva | 100 % | 100 % |
| total_ttc | 100 % | 100 % |
| nb_lignes | 100 % | 100 % |
| **Global** | **320/320 = 100 %** | **315/320 = 98,4 %** |
| Temps moyen par page (étape 2) | ≈ 7–8 s | ≈ 6 s |

**Erreurs à 768 px**

| Facture | Champ | Attendu | Lu | Type d'erreur |
|---|---|---|---|---|
| LG-2025-0008 | numéro | `LG-2025-0008` | `LO-2025-0008` | lettre confondue (G → O) |
| V-2025-0001 | SIRET fournisseur | `38849696066978` | `38849696068978` | chiffre confondu (6 → 8) |
| V-2025-0009 | TVA intracom. | `FR48388496960` | `FR46388496960` | chiffre confondu (8 → 6) |
| MS-2026-0005 | SIRET client | `45271111204945` | `4527111204945` | chiffre **oublié** dans une suite de « 1 » |
| P-2025-0002 | IBAN | `FR2142784980841241182449353` | `FR2142794960841241182449353` | 2 chiffres confondus (8 → 9, 8 → 6) |

**Analyse : quels champs sont les plus fragiles, et pourquoi ?**

- **Les identifiants longs** (SIRET, TVA, IBAN, numéro de facture) concentrent **toutes** les erreurs. Ce sont de longues suites de caractères sans signification : le modèle ne peut pas s'aider du contexte ou du vocabulaire pour « deviner » un caractère flou, contrairement à un mot comme « Informatique ». Les confusions observées sont classiques entre caractères de forme proche (6/8, 8/9, G/O). Une suite de chiffres identiques (« 1111 ») fait aussi perdre le compte.
- **Les montants et les dates** restent justes à 768 px. Ils sont plus courts, écrits en plus gros (les totaux sont en gras), et ont un format très contraint (`jj/mm/aaaa`, `1 234,56 €`).
- **Les champs textuels** (noms, catégorie, statut, mode de paiement) sont robustes : ce sont des mots courants que le modèle reconnaît même flous.
- **Erreur détectée ou silencieuse ?** Sur les 5 erreurs, une seule serait repérée par nos contrôles qualité de l'étape 4 : le SIRET client à 13 chiffres ne passe pas la règle « 14 chiffres ». Les 4 autres gardent un format valide : elles sont **silencieuses**, et c'est le cas le plus dangereux, en particulier pour un IBAN faux qui pourrait envoyer un paiement au mauvais compte. Des contrôles supplémentaires pourraient en attraper certaines, comme la clé de contrôle d'un IBAN (modulo 97) ou d'un SIRET (algorithme de Luhn).

**Conclusion.** La résolution recommandée par la fiche du modèle (**1540 px**) donne **100 %** sur l'échantillon. Passer à 768 px fait gagner environ 20 % de temps, mais introduit des erreurs sur les champs les plus critiques. On garde donc **1540 px** : sur des données financières, une erreur silencieuse sur un IBAN coûte bien plus cher que quelques secondes d'OCR.

## 4. Proposition d'industrialisation

**Objectif** : passer de 100 factures traitées à la main sur un poste à **plusieurs milliers de factures par mois**, avec plusieurs utilisateurs qui interrogent l'agent.

**Ordre de grandeur.** Prenons 5 000 factures par mois, dont la moitié de scans, comme dans notre jeu. Cela fait 2 500 pages à OCRiser. À 7,2 s par page en séquentiel (mesuré), il faudrait **5 heures** d'OCR par mois, en un seul bloc si tout arrive en fin de mois. Le reste du pipeline est négligeable : la couche texte se lit en quelques millisecondes, et le silver complet se reconstruit en quelques secondes. **L'OCR est donc le goulot d'étranglement**, et la deuxième question est de faire tourner tout cela sans intervention humaine.

Nous avons classé les sujets par **ordre de priorité**, selon le risque qu'ils font courir s'ils ne sont pas traités.

### Priorité 1 — Automatiser et déployer le pipeline *(implémenté en partie)*

*Pourquoi d'abord ?* Aujourd'hui, il faut lancer trois commandes à la main sur un poste précis. Une chaîne qui dépend d'une personne et d'une machine n'est pas un système de production.

**Ce que nous avons implémenté :**

- **Une commande unique** `factures pipeline` qui enchaîne récupération → bronze → silver et affiche la durée totale. Elle renvoie un **code de sortie non nul s'il reste des documents en erreur** : n'importe quel planificateur (cron, Kubernetes, Airflow…) sait ainsi qu'il doit alerter.
- **Une image Docker** (`Dockerfile`) : Python 3.12 et dépendances verrouillées par `uv.lock`, exécution avec un utilisateur sans privilèges, et **aucun secret dans l'image**, puisque les identifiants sont fournis au lancement (`docker run --env-file .env factures`). Test réalisé : l'image démarre d'une zone d'arrivée vide, retélécharge les 100 PDF, **ne refait aucun OCR** grâce aux empreintes déjà présentes dans le bronze, reconstruit le silver en 23 s et se termine avec le code 0. Le conteneur est donc **sans état** : l'état est dans S3, et on peut le lancer n'importe où, le détruire et le relancer.

**Ce qu'il resterait à faire :** publier l'image dans le registre Hyperfluid (*Registry / Pithos*) et la lancer comme **tâche planifiée** sur *Containers / CaaSThor*, par exemple toutes les heures. Plus tard, on pourrait déclencher le traitement à chaque dépôt de fichier dans le bucket, grâce aux notifications S3, au lieu de le planifier. Il faut enfin garantir **un seul pipeline à la fois**, avec un verrou de tâche, pour éviter que deux exécutions écrivent le silver en même temps.

### Priorité 2 — Faire monter l'OCR en charge *(implémenté en partie)*

*Pourquoi ?* C'est le goulot mesuré : 5 heures par mois en séquentiel.

**Ce que nous avons implémenté :** l'extraction de la couche bronze se fait **en parallèle** (`ThreadPoolExecutor`), avec un nombre d'appels simultanés réglable par configuration (`OCR_PARALLELISME`, 4 par défaut). L'OCR passe la plupart de son temps à **attendre** la réponse du GPU distant : plusieurs requêtes simultanées occupent mieux ce GPU.

| Appels OCR simultanés | 12 pages | Temps moyen par page | Gain |
|---|---|---|---|
| 1 (avant) | 86,2 s | 7,18 s | — |
| 4 (par défaut) | 26,9 s | 2,24 s | × 3,2 |
| 8 | 21,5 s | 1,79 s | × 4,0 |

Avec 8 appels simultanés, les 5 heures mensuelles deviennent **environ 1 h 15**. Le gain diminue au-delà de 4, parce que le GPU du modèle (partagé avec les autres utilisateurs de la plateforme) sature. Pour aller plus loin, il faut **plus de GPU**, c'est-à-dire plusieurs instances du modèle derrière la passerelle, et non plus de parallélisme côté client.

**Ce qu'il resterait à faire :**

- une **file d'attente** (une tâche par page) consommée par plusieurs *workers*, qui peut absorber un pic de fin de mois ;
- un **débit maximal** aligné sur le quota de la clé d'API, avec des nouvelles tentatives espacées de plus en plus (*backoff exponentiel*) en cas d'erreur 429 ou 503 ;
- un **nombre maximal de tentatives** : aujourd'hui, un document en erreur est retenté indéfiniment ; au-delà de N échecs, il faudrait l'envoyer en quarantaine pour qu'un humain le traite.

### Priorité 3 — Sécuriser les accès et les secrets

*Pourquoi ?* Les données contiennent des IBAN et des SIRET, et le système devient multi-utilisateurs.

- **Secrets** : remplacer le fichier `.env` par un **gestionnaire de secrets** (secrets Kubernetes, Vault, ou le coffre de la plateforme), injectés dans les conteneurs au lancement, avec **rotation** régulière des clés.
- **Une identité par composant, avec le moindre privilège.** Le pipeline aurait l'écriture sur bronze (sans suppression, déjà le cas) et sur silver. Le serveur MCP et l'agent n'auraient que des **clés en lecture seule**. Cette leçon vient directement de l'étape 7 : avec une clé qui avait l'écriture, une requête `COPY` est passée tant que le filtrage SQL n'était pas en place. Une clé en lecture seule aurait bloqué l'écriture au niveau de la plateforme, indépendamment de notre code.
- **Multi-utilisateurs** : exposer le serveur MCP en HTTP (transport *streamable HTTP*) derrière une **authentification** (OAuth / SSO de l'entreprise), et **journaliser chaque requête SQL** avec l'utilisateur qui l'a déclenchée, pour l'audit. Si besoin, **masquer les IBAN** dans les réponses aux utilisateurs qui n'ont pas à les voir.

### Priorité 4 — Supervision et gestion des erreurs

*Pourquoi ?* Un pipeline automatique qui échoue en silence est pire qu'un traitement manuel.

- Publier à chaque exécution des **indicateurs** : documents reçus, ingérés et en erreur, durée et coût de l'OCR, nombre de rejets en quarantaine, nombre de factures signalées. Ils alimenteraient un tableau de bord (*Monitoring* Hyperfluid).
- **Alerter** sur un code de sortie non nul (déjà fourni par `factures pipeline`), sur une hausse anormale des erreurs OCR ou des rejets, ou sur une absence de nouvelles factures pendant plusieurs jours.
- Mettre en place un **circuit de traitement de la quarantaine** et des factures signalées : un humain les vérifie et les corrige à la source. Ces cas doivent déclencher une action, pas seulement remplir une table.

### Priorité 5 — Tests et non-régression

- **Intégration continue** : à chaque commit, lancer les tests du parseur sur des exemples de textes natifs et OCR, et le script de test du serveur MCP (`tests/test_mcp.py`).
- **Non-régression de la qualité OCR** : relancer `factures evaluer` sur la vérité terrain à chaque changement de modèle, de résolution ou de parseur, et bloquer la mise en production si la précision baisse. La vérité terrain doit aussi s'agrandir régulièrement avec des factures réelles vérifiées.

### Ce qui n'est pas prioritaire maintenant

- **Le volume de stockage** : quelques milliers de factures par mois représentent quelques Mo de Parquet, et DuckDB les lit en quelques secondes. Le jour où le silver deviendrait trop gros pour être reconstruit entièrement, on passerait à un **silver incrémental partitionné par mois**, ou à un format de table comme Iceberg ou Delta, que DuckDB sait lire. On **compacterait** aussi les petits fichiers du bronze, puisque chaque exécution en ajoute un.
- **Le coût** : c'est surtout le GPU de l'OCR, seulement pour les scans puisque les PDF natifs n'en ont pas besoin, et les appels au LLM de l'agent. Les résultats des outils MCP sont exacts et rapides. On pourrait **mettre en cache** les réponses aux questions fréquentes et garder un **modèle local** pour le développement, gratuit et sans envoi de données à l'extérieur.

## 5. Difficultés, limites et pistes d'amélioration

- **Droits de la clé d'API OCR.** Nous avons d'abord créé une clé avec le seul scope `model:inference`, pour appliquer le moindre privilège. Les appels au modèle partagé `lightonocr-3-1b-test` étaient refusés (`403 – API key does not have access to this model`), alors que la clé atteignait bien la passerelle (colonne « Last Used » renseignée). Une clé `model:*` fonctionne. Le risque est limité : la clé reste dans le `.env` local et peut être révoquée depuis la console.
- **Une clé S3 par bucket.** Les clés Hyperfluid sont confinées à un bucket : il a fallu prévoir un jeu de variables par bucket (préfixes `SOURCE_`, `BRONZE_`, `SILVER_`).
- **URL de l'OCR.** La console affiche l'URL complète `…/v1/chat/completions`, alors que le SDK OpenAI attend l'URL de base qui se termine par `/v1`.
