"""Configuration du projet, lue depuis le fichier .env (jamais versionné)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Racine du projet = dossier qui contient pyproject.toml
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
LANDING_DIR = DATA_DIR / "landing"  # zone d'arrivée des PDF


def _require(name: str) -> str:
    """Lit une variable du .env et s'arrête avec un message clair si elle est vide."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Variable {name} manquante : complétez votre fichier .env "
            f"(voir .env.example)."
        )
    return value


@dataclass(frozen=True)
class S3Target:
    """Un bucket S3 et la clé qui y donne accès.

    Sur Hyperfluid, une clé S3 est confinée à un seul bucket : chaque bucket a
    donc son propre jeu de variables, distingué par un préfixe (SOURCE_, BRONZE_…).
    """

    bucket: str
    access_key: str
    secret_key: str
    endpoint_url: str
    region: str

    @classmethod
    def from_env(cls, prefix: str) -> "S3Target":
        """Construit la config d'un bucket à partir des variables PREFIX_... du .env."""
        p = prefix.upper()
        return cls(
            bucket=_require(f"{p}_BUCKET"),
            access_key=_require(f"{p}_AWS_ACCESS_KEY_ID"),
            secret_key=_require(f"{p}_AWS_SECRET_ACCESS_KEY"),
            endpoint_url=_require(f"{p}_AWS_ENDPOINT_URL"),
            region=os.getenv(f"{p}_AWS_DEFAULT_REGION", "").strip() or "us-east-1",
        )
