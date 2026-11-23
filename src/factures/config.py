"""Configuration lue dans le fichier .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
LANDING_DIR = DATA_DIR / "landing"


def variable(name: str) -> str:
    """Lit une variable du .env et s'arrête avec un message clair si elle est vide."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variable {name} manquante : complétez votre fichier .env (voir .env.example).")
    return value


@dataclass(frozen=True)
class S3Target:
    """Un bucket S3 et la clé qui y donne accès (une clé par bucket sur Hyperfluid)."""

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
            bucket=variable(f"{p}_BUCKET"),
            access_key=variable(f"{p}_AWS_ACCESS_KEY_ID"),
            secret_key=variable(f"{p}_AWS_SECRET_ACCESS_KEY"),
            endpoint_url=variable(f"{p}_AWS_ENDPOINT_URL"),
            region=os.getenv(f"{p}_AWS_DEFAULT_REGION", "").strip() or "us-east-1",
        )
