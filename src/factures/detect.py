"""Étape 1b — déterminer si un PDF est « texte » (couche texte) ou « image » (OCR nécessaire).

On lit la couche texte avec PyMuPDF et on compte les caractères alphanumériques.
Un PDF scanné n'est qu'une image : sa couche texte est vide.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pymupdf

from .config import LANDING_DIR

# En dessous de ce nombre de caractères, le PDF n'a pas de texte exploitable.
MIN_CARACTERES = 30


def classify_pdf(path: Path) -> dict:
    """Compte les caractères de la couche texte d'un PDF et le classe « texte » ou « image »."""
    with pymupdf.open(path) as doc:
        texte = "".join(page.get_text() for page in doc)
    nb = sum(c.isalnum() for c in texte)
    return {
        "fichier": path.name,
        "caracteres": nb,
        "type": "texte" if nb >= MIN_CARACTERES else "image",
    }


def detect_all(folder: Path = LANDING_DIR) -> list[dict]:
    """Classe tous les PDF de la zone d'arrivée et écrit le résultat dans detection.csv."""
    results = [classify_pdf(p) for p in sorted(folder.glob("*.pdf"))]
    with (folder / "detection.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["fichier", "caracteres", "type"])
        writer.writeheader()
        writer.writerows(results)
    return results
