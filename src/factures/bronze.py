"""Étape 3 — couche bronze : texte brut extrait de chaque PDF + métadonnées de traçabilité.

Principes :
- un PDF est identifié par l'empreinte SHA-256 de son contenu (pas par son nom) ;
- incrémental : un PDF déjà ingéré avec succès n'est jamais retraité ni ré-OCRisé ;
- si l'OCR échoue, l'erreur est enregistrée et le PDF sera retenté au prochain lancement ;
- la couche bronze est en ajout seul : chaque exécution écrit un NOUVEAU fichier
  Parquet dans le bucket, on ne modifie ni ne supprime jamais les fichiers existants.
"""

from __future__ import annotations

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

from .config import LANDING_DIR, S3Target
from .detect import classify_pdf
from .ocr import TAILLE_MAX_PX, ocr_page
from .storage import list_objects, s3_client

PREFIXE = "documents/"  # dossier des fichiers Parquet dans le bucket bronze

SCHEMA = pa.schema(
    [
        ("sha256", pa.string()),  # identifiant du document (empreinte du contenu)
        ("fichier", pa.string()),  # nom du PDF
        ("s3_uri", pa.string()),  # emplacement d'origine dans le bucket source
        ("s3_etag", pa.string()),
        ("taille_octets", pa.int64()),
        ("type_pdf", pa.string()),  # "texte" ou "image" (étape 1)
        ("methode", pa.string()),  # "couche_texte" ou "ocr"
        ("modele_ocr", pa.string()),  # nom du modèle si OCR
        ("taille_image_px", pa.int32()),  # résolution envoyée à l'OCR
        ("texte", pa.string()),  # texte brut extrait
        ("duree_s", pa.float64()),  # temps d'extraction
        ("statut", pa.string()),  # "ok" ou "erreur"
        ("erreur", pa.string()),  # message d'erreur si statut = erreur
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
    """Renvoie les empreintes des PDF déjà ingérés avec succès (statut ok)."""
    table = lire_bronze(target).to_pylist()
    return {ligne["sha256"] for ligne in table if ligne["statut"] == "ok"}


def extraire(path: Path, type_pdf: str) -> dict:
    """Extrait le texte d'un PDF : couche texte si possible, sinon OCR. Ne lève pas d'erreur."""
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
            ligne.update(methode="ocr", modele_ocr=os.environ["OCR_MODEL"],
                         taille_image_px=TAILLE_MAX_PX, texte="\n\n".join(textes), duree_s=duree)
    except Exception as e:  # OCR indisponible, PDF illisible… on trace et on continue
        ligne.update(statut="erreur", erreur=f"{type(e).__name__}: {e}"[:500])
    return ligne


def traiter_pdf(path: Path, empreinte: str, infos_s3: dict) -> dict:
    """Extrait le texte d'un PDF et construit sa ligne bronze (métadonnées + texte)."""
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
    print(f"  {ligne['statut']:<6} {ligne['methode'] or '-':<12} {path.name}"
          + (f"  ({ligne['erreur'][:80]})" if ligne["erreur"] else ""), flush=True)
    return ligne


def ingerer(dossier: Path = LANDING_DIR) -> dict:
    """Ingère dans la couche bronze les PDF de la zone d'arrivée pas encore traités."""
    target = S3Target.from_env("BRONZE")
    deja = deja_ingeres(target)
    manifeste = json.loads((dossier / "_manifest.json").read_text())
    infos_s3 = {v["local_path"]: v for v in manifeste.values()}

    # 1) Sélection des PDF à traiter (rapide) : on écarte ceux déjà ingérés
    a_traiter, stats = [], {"ignores": 0, "ok": 0, "erreurs": 0}
    for path in sorted(dossier.glob("*.pdf")):
        empreinte = sha256_fichier(path)
        if empreinte in deja:
            stats["ignores"] += 1
            continue
        deja.add(empreinte)  # évite un doublon si le même PDF est présent sous 2 noms
        a_traiter.append((path, empreinte))

    # 2) Extraction en parallèle : l'OCR (~7 s/page) attend surtout le réseau et le GPU distant,
    #    donc plusieurs appels simultanés divisent le temps total.
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
    print(f"→ {len(lignes)} lignes écrites dans s3://{target.bucket}/{cle}")
