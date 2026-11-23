"""Étape 6 : entrepôt DuckDB, avec des vues SQL sur les fichiers Parquet de S3."""

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
    """Génère les instructions SQL qui créent les vues bronze et silver sur les fichiers S3."""
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
    """Renvoie les instructions SQL qui préparent une connexion (extension, clés, vues)."""
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
