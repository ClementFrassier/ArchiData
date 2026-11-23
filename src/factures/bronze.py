"""Étape 3 : couche bronze (texte brut extrait de chaque PDF et métadonnées)."""

import hashlib
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf

from .config import LANDING_DIR, S3Target, variable
from .detect import classify_pdf
from .ocr import TAILLE_MAX_PX, ocr_page
from .storage import list_objects, s3_client

PREFIXE = "documents/"

SCHEMA = pa.schema(
    [
        ("sha256", pa.string()),
        ("fichier", pa.string()),
        ("s3_uri", pa.string()),
        ("s3_etag", pa.string()),
        ("taille_octets", pa.int64()),
        ("type_pdf", pa.string()),
        ("methode", pa.string()),
        ("modele_ocr", pa.string()),
        ("taille_image_px", pa.int32()),
        ("texte", pa.string()),
        ("duree_s", pa.float64()),
        ("statut", pa.string()),
        ("erreur", pa.string()),
        ("ingere_le", pa.timestamp("us", tz="UTC")),
    ]
)


def sha256_fichier(path: Path) -> str:
    """Calcule l'empreinte SHA-256 du contenu d'un fichier."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lire_bronze(target: S3Target) -> pa.Table:
    """Lit et assemble tous les fichiers Parquet déjà présents dans la couche bronze."""
    client = s3_client(target)
    tables = []
    for obj in list_objects(target, PREFIXE):
        data = client.get_object(Bucket=target.bucket, Key=obj["Key"])["Body"].read()
        tables.append(pq.read_table(io.BytesIO(data)))
    return pa.concat_tables(tables) if tables else SCHEMA.empty_table()


def deja_ingeres(target: S3Target) -> set[str]:
    """Renvoie les empreintes des PDF déjà ingérés avec succès."""
    table = lire_bronze(target).to_pylist()
    return {ligne["sha256"] for ligne in table if ligne["statut"] == "ok"}


def extraire(path: Path, type_pdf: str) -> dict:
    """Extrait le texte d'un PDF (couche texte ou OCR) et enregistre l'erreur éventuelle."""
    ligne = {"methode": None, "modele_ocr": None, "taille_image_px": None,
             "texte": None, "duree_s": None, "statut": "ok", "erreur": None}
    try:
        if type_pdf == "texte":
            debut = time.perf_counter()
            with pymupdf.open(path) as doc:
                ligne["texte"] = "".join(page.get_text() for page in doc)
            ligne.update(methode="couche_texte", duree_s=time.perf_counter() - debut)
        else:
            textes, duree = [], 0.0
            with pymupdf.open(path) as doc:
                nb_pages = len(doc)
            for num in range(nb_pages):
                texte, d = ocr_page(path, num, TAILLE_MAX_PX)
                textes.append(texte)
                duree += d
            ligne.update(methode="ocr", modele_ocr=variable("OCR_MODEL"),
                         taille_image_px=TAILLE_MAX_PX, texte="\n\n".join(textes), duree_s=duree)
    except Exception as e:
        ligne.update(statut="erreur", erreur=f"{type(e).__name__}: {e}"[:500])
    return ligne


def traiter_pdf(path: Path, empreinte: str, infos_s3: dict) -> dict:
    """Extrait le texte d'un PDF et construit sa ligne bronze."""
    type_pdf = classify_pdf(path)["type"]
    s3 = infos_s3.get(path.name, {})
    ligne = {
        "sha256": empreinte,
        "fichier": path.name,
        "s3_uri": s3.get("s3_uri"),
        "s3_etag": s3.get("etag"),
        "taille_octets": path.stat().st_size,
        "type_pdf": type_pdf,
        **extraire(path, type_pdf),
        "ingere_le": datetime.now(timezone.utc),
    }
    message = f"  {ligne['statut']:<6} {ligne['methode'] or '-':<12} {path.name}"
    if ligne["erreur"]:
        message += f"  ({ligne['erreur'][:80]})"
    print(message, flush=True)
    return ligne


def ingerer() -> dict:
    """Ingère dans la couche bronze les PDF de la zone d'arrivée pas encore traités."""
    target = S3Target.from_env("BRONZE")
    deja = deja_ingeres(target)
    manifeste = json.loads((LANDING_DIR / "_manifest.json").read_text())
    infos_s3 = {v["local_path"]: v for v in manifeste.values()}

    a_traiter, stats = [], {"ignores": 0, "ok": 0, "erreurs": 0}
    for path in sorted(LANDING_DIR.glob("*.pdf")):
        empreinte = sha256_fichier(path)
        if empreinte in deja:
            stats["ignores"] += 1
            continue
        deja.add(empreinte)
        a_traiter.append((path, empreinte))

    parallelisme = int(os.getenv("OCR_PARALLELISME", "4"))
    with ThreadPoolExecutor(max_workers=parallelisme) as pool:
        lignes = list(pool.map(lambda pe: traiter_pdf(pe[0], pe[1], infos_s3), a_traiter))

    for ligne in lignes:
        stats["ok" if ligne["statut"] == "ok" else "erreurs"] += 1

    if lignes:
        ecrire_lot(target, lignes)
    return stats


def ecrire_lot(target: S3Target, lignes: list[dict]) -> None:
    """Écrit les nouvelles lignes dans un nouveau fichier Parquet du bucket bronze."""
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(lignes, schema=SCHEMA), buffer)
    horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cle = f"{PREFIXE}lot_{horodatage}.parquet"
    s3_client(target).put_object(Bucket=target.bucket, Key=cle, Body=buffer.getvalue())
    print(f"{len(lignes)} lignes écrites dans s3://{target.bucket}/{cle}")
