# Factures : de la facture scannée à l'agent IA

Chaîne de traitement de bout en bout :

```
PDF (bucket S3) ──► détection texte/image ──► couche texte ou OCR LightOnOCR-3
    ──► BRONZE (texte brut, Parquet S3) ──► SILVER (tables typées + quarantaine, Parquet S3)
    ──► entrepôt DuckDB (vues SQL) ──► serveur MCP (lecture seule) ──► agent IA (LLM au choix)
```

Le rapport (choix techniques, résultats, industrialisation) est dans [`rapport.md`](rapport.md).

---

## 1. Prérequis

- **Linux, macOS ou WSL** avec **[uv](https://docs.astral.sh/uv/)**, qui installe aussi Python 3.12 automatiquement :
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- Un accès à la console **Hyperfluid** (`dams-5`) pour créer les clés d'accès.
- *(facultatif)* Docker, pour lancer le pipeline en conteneur ; DBeaver, pour consulter l'entrepôt ; Ollama, pour un LLM local.

## 2. Installation

```bash
git clone <url-du-dépôt> factures && cd factures
uv sync                 # installe Python et toutes les dépendances (versions verrouillées dans uv.lock)
cp .env.example .env    # puis remplir .env (section 3)
```

## 3. Configuration : le fichier `.env`

Toute la configuration et **tous les secrets** sont dans `.env`. Ce fichier est ignoré par Git : **ne jamais le committer**. Le modèle à recopier est [`.env.example`](.env.example).

### Clés S3 : une par bucket

Sur Hyperfluid, **une clé S3 ne donne accès qu'à un seul bucket**. Pour chaque bucket, il faut aller dans *Storage → Buckets → (bucket) → Access → New key*. La console fournit 4 lignes `AWS_…`. Colle-les dans le bloc du bucket **en ajoutant le préfixe** :

| Bucket | Préfixe | Droits nécessaires |
|---|---|---|
| `factures` (PDF source) | `SOURCE_` | lecture |
| `default-bronze` | `BRONZE_` | lecture + écriture |
| `default-silver` | `SILVER_` | lecture + écriture |

Exemple : `AWS_ACCESS_KEY_ID=…` devient `SOURCE_AWS_ACCESS_KEY_ID=…`.

### OCR et LLM

Dans *AI Gateway → API Keys → Create API Key*, crée une clé avec le scope **`model:*`**. Le scope `model:inference` est refusé pour les modèles partagés.

| Variable | Valeur |
|---|---|
| `OCR_ENDPOINT` | « Gateway URL » du modèle `lightonocr-3-1b-test` (onglet *Endpoint*), **sans** `/chat/completions` : elle doit finir par `/v1` |
| `OCR_API_KEY` | la clé créée ci-dessus |
| `OCR_MODEL` | `lightonocr-3-1b-test` |
| `LLM_BASE_URL` | même URL que l'OCR, en remplaçant `lightonocr-3-1b-test` par `granite-4-2-8b` |
| `LLM_API_KEY` | la même clé |
| `LLM_MODEL` | `granite-4-2-8b` |

Réglage facultatif : `OCR_PARALLELISME` (nombre d'appels OCR simultanés, 4 par défaut).

## 4. Lancer le pipeline

Tout en une commande (récupération → bronze → silver) :

```bash
uv run factures pipeline
```

Ou étape par étape :

| Commande | Étape | Ce qu'elle fait |
|---|---|---|
| `uv run factures fetch` | 1 | télécharge les nouvelles factures du bucket dans `data/landing/` |
| `uv run factures detect` | 1 | classe chaque PDF « texte » ou « image » (`data/landing/detection.csv`) |
| `uv run factures ocr <fichier.pdf> [--taille 1540]` | 2 | envoie une page au modèle d'OCR, affiche le texte et le temps |
| `uv run factures bronze` | 3 | extrait le texte des nouveaux PDF (OCR si besoin) vers la couche bronze ; incrémental |
| `uv run factures silver` | 4 | reconstruit les tables silver (factures, lignes, quarantaine) depuis le bronze |
| `uv run factures evaluer [--taille 768]` | 5 | précision de l'OCR champ par champ face à `evaluation/verite_terrain.csv` |
| `uv run factures sql "SELECT …"` | 6 | requête SQL sur l'entrepôt (schémas `bronze` et `silver`) |
| `uv run factures dbeaver` | 6 | génère `data/dbeaver_init.sql` pour DBeaver |

Le premier lancement OCRise les 50 factures scannées : environ 2 minutes avec 4 appels en parallèle. Les lancements suivants ne retraitent que les nouveaux fichiers.

## 5. Interroger les données

**En SQL depuis le terminal :**
```bash
uv run factures sql "SELECT statut_paiement, count(*), sum(total_ttc) FROM silver.factures GROUP BY 1"
```

**Avec DBeaver :**
1. Lance `uv run factures dbeaver`, puis copie le script (sous WSL : `cat data/dbeaver_init.sql | clip.exe`).
2. Dans DBeaver : *Nouvelle connexion → DuckDB*, *Path* = `:memory:`.
3. Ouvre un éditeur SQL, colle le script et exécute-le en entier (**Alt+X**).
4. Les schémas `bronze` et `silver` sont alors disponibles.

Le script contient les clés S3 : il reste dans `data/`, qui est ignoré par Git.

## 6. Serveur MCP et agent

**Tester le serveur MCP** (appelle chaque outil et vérifie que toute modification est refusée) :
```bash
uv run python tests/test_mcp.py
```

**Avec Claude Code** : le fichier `.mcp.json` déclare le serveur `factures`. Ouvre une session Claude Code dans le dossier du projet, accepte le serveur, puis pose une question, par exemple « Quel est le montant TTC des factures en retard en 2025 ? ».

**Avec notre agent :**
```bash
uv run factures-agent          # conversation ; « quitter » pour arrêter
uv run factures-agent "Quel est le montant TTC des factures en retard en 2025 ?"
```

Le LLM se choisit par configuration (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). Pour essayer un autre modèle sans modifier le `.env` (ici, un modèle local Ollama ; sous WSL, l'adresse de Windows est donnée par `ip route | awk '/default/{print $3}'`) :
```bash
LLM_BASE_URL=http://172.19.224.1:11434/v1 LLM_MODEL=llama3.2:3b uv run factures-agent "…"
```

## 7. Lancer le pipeline avec Docker

```bash
docker build -t factures .
docker run --rm --env-file .env factures            # = factures pipeline
docker run --rm --env-file .env factures silver     # n'importe quelle sous-commande
```

L'image ne contient aucun secret : ils sont fournis au lancement par `--env-file`.

## 8. Organisation du projet

```
src/factures/
  config.py       configuration (.env), une clé S3 par bucket
  storage.py      client S3
  fetch.py        étape 1 — récupération incrémentale des PDF
  detect.py       étape 1 — détection texte / image
  ocr.py          étape 2 — appel du modèle LightOnOCR-3
  bronze.py       étape 3 — couche bronze (texte brut + traçabilité)
  parsing.py      étape 4 — lecture des champs (texte natif et OCR)
  silver.py       étape 4 — tables silver, contrôles qualité, quarantaine
  evaluation.py   étape 5 — précision de l'OCR face à la vérité terrain
  entrepot.py     étape 6 — DuckDB : vues SQL sur les fichiers S3
  mcp_server.py   étape 7 — serveur MCP en lecture seule
  agent.py        étape 8 — agent conversationnel
  __init__.py     commandes `factures …`
tests/test_mcp.py           test de tous les outils MCP
evaluation/                 vérité terrain et résultats de l'évaluation OCR
Dockerfile                  image du pipeline (étape 9)
```

## 9. Problèmes fréquents

| Message | Cause et solution |
|---|---|
| `Variable … manquante` | une ligne du `.env` est vide ou n'a pas le bon préfixe (`SOURCE_`, `BRONZE_`, `SILVER_`) |
| `AccessDenied` sur `PutObject` | la clé S3 de ce bucket est en lecture seule : la recréer en lecture + écriture |
| `403 – API key does not have access to this model` | la clé d'API a le scope `model:inference` : en créer une en `model:*` |
| L'agent local ne répond pas | Ollama tourne sous Windows : vérifier son adresse avec `ip route` et qu'Ollama est lancé |
