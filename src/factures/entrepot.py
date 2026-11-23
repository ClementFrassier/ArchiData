"""Étape 6 — l'entrepôt de données : rendre bronze et silver interrogeables en SQL.

Séparation stockage / calcul :
- STOCKAGE : les fichiers Parquet dans les buckets S3 (un seul exemplaire des données) ;
- CALCUL   : DuckDB, un moteur SQL qui lit ces fichiers à la demande. L'entrepôt ne
  contient que des VUES (des requêtes enregistrées), aucune donnée copiée.

Chaque programme ouvre sa propre connexion DuckDB en mémoire : il n'y a pas de
fichier de base partagé, donc pas de verrou. Plusieurs programmes (et DBeaver)
peuvent interroger les mêmes données en même temps sans se bloquer.

Les vues pointent vers des adresses s3://… et non vers des chemins locaux : elles
restent valides sur n'importe quelle machine, dans un conteneur ou chez un collègue,
du moment que les identifiants sont dans son .env.
"""

from __future__ import annotations

import duckdb

from .config import S3Target


def sql_secret(nom: str, target: S3Target) -> str:
    """Génère l'instruction SQL qui donne à DuckDB la clé d'accès d'un bucket."""
    hote = target.endpoint_url.removeprefix("https://").removeprefix("http://").rstrip("/")
    return (
        f"CREATE OR REPLACE SECRET {nom} (TYPE s3, KEY_ID '{target.access_key}', "
        f"SECRET '{target.secret_key}', ENDPOINT '{hote}', REGION '{target.region}', "
        f"URL_STYLE 'path', USE_SSL {str(target.endpoint_url.startswith('https')).lower()}, "
        f"SCOPE 's3://{target.bucket}')"
    )


def sql_vues(bronze: S3Target, silver: S3Target) -> list[str]:
    """Génère les instructions SQL qui créent les vues bronze.* et silver.* sur les fichiers S3."""
    b, s = f"s3://{bronze.bucket}", f"s3://{silver.bucket}"
    return [
        "CREATE SCHEMA IF NOT EXISTS bronze",
        "CREATE SCHEMA IF NOT EXISTS silver",
        f"CREATE OR REPLACE VIEW bronze.documents AS SELECT * FROM read_parquet('{b}/documents/*.parquet')",
        f"CREATE OR REPLACE VIEW silver.factures AS SELECT * FROM read_parquet('{s}/factures/*.parquet')",
        f"CREATE OR REPLACE VIEW silver.lignes_facture AS SELECT * FROM read_parquet('{s}/lignes_facture/*.parquet')",
        f"CREATE OR REPLACE VIEW silver.quarantaine AS SELECT * FROM read_parquet('{s}/quarantaine/*.parquet')",
    ]


def script_initialisation() -> list[str]:
    """Renvoie toutes les instructions SQL pour préparer une connexion (extension, clés, vues)."""
    bronze, silver = S3Target.from_env("BRONZE"), S3Target.from_env("SILVER")
    return [
        "INSTALL httpfs",
        "LOAD httpfs",
        sql_secret("cle_bronze", bronze),
        sql_secret("cle_silver", silver),
        *sql_vues(bronze, silver),
    ]


def connexion() -> duckdb.DuckDBPyConnection:
    """Ouvre une connexion DuckDB en mémoire, prête à interroger bronze et silver."""
    con = duckdb.connect(":memory:")
    for instruction in script_initialisation():
        con.execute(instruction)
    return con
