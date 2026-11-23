"""Étape 1 : récupération incrémentale des factures du bucket S3."""

import json
from pathlib import Path

from .config import LANDING_DIR, S3Target
from .storage import list_objects, s3_client

MANIFEST = LANDING_DIR / "_manifest.json"


def _load_manifest() -> dict:
    """Charge la liste des fichiers déjà téléchargés (vide au premier lancement)."""
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {}


def _save_manifest(manifest: dict) -> None:
    """Enregistre la liste des fichiers téléchargés dans _manifest.json."""
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(MANIFEST)


def fetch() -> dict:
    """Télécharge les PDF nouveaux ou modifiés du bucket et renvoie des compteurs."""
    target = S3Target.from_env("SOURCE")
    LANDING_DIR.mkdir(parents=True, exist_ok=True)
    client = s3_client(target)
    manifest = _load_manifest()

    stats = {"vus": 0, "telecharges": 0, "deja_presents": 0}
    for obj in list_objects(target):
        key = obj["Key"]
        if not key.lower().endswith(".pdf"):
            continue
        stats["vus"] += 1
        etag = obj["ETag"].strip('"')
        local = LANDING_DIR / Path(key).name
        known = manifest.get(key)

        if known and known["etag"] == etag and local.exists():
            stats["deja_presents"] += 1
            continue

        client.download_file(target.bucket, key, str(local))
        manifest[key] = {"etag": etag, "s3_uri": f"s3://{target.bucket}/{key}", "local_path": local.name}
        _save_manifest(manifest)
        stats["telecharges"] += 1
        print(f"  téléchargé : {key}")

    return stats
