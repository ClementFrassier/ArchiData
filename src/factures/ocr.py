"""Étape 2 — envoyer une page de facture au modèle d'OCR LightOnOCR-3.

Le modèle est exposé derrière une API compatible OpenAI : on utilise donc le SDK
`openai` standard. L'adresse, la clé et le nom du modèle viennent du .env, ce qui
permet de changer de fournisseur d'OCR sans modifier le code.

Recommandations de la fiche du modèle (model card) :
- une seule image par requête, encodée en PNG base64, sans texte d'instruction ;
- page rendue avec un côté le plus long de 1540 px, proportions conservées ;
- temperature 0.2, top_p 0.9, max_tokens 4096.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pymupdf
from openai import OpenAI

from . import config  # noqa: F401  (charge le .env)

TAILLE_MAX_PX = 1540  # côté le plus long recommandé par la fiche du modèle


def page_to_png(pdf_path: Path, page_num: int = 0, taille_max_px: int = TAILLE_MAX_PX) -> bytes:
    """Convertit une page du PDF en image PNG dont le plus grand côté fait taille_max_px."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_num]
        zoom = taille_max_px / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")


def ocr_png(png: bytes) -> str:
    """Envoie une image PNG au modèle d'OCR et renvoie le texte reconnu (markdown)."""
    client = OpenAI(
        base_url=os.environ["OCR_ENDPOINT"],
        api_key=os.environ["OCR_API_KEY"],
        timeout=120,  # secondes : on n'attend pas indéfiniment si le service est bloqué
        max_retries=2,  # 2 nouvelles tentatives en cas d'erreur réseau passagère
    )
    image_b64 = base64.b64encode(png).decode("utf-8")
    response = client.chat.completions.create(
        model=os.environ["OCR_MODEL"],
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                ],
            }
        ],
        temperature=0.2,
        top_p=0.9,
        max_tokens=4096,
    )
    return response.choices[0].message.content or ""


def ocr_page(pdf_path: Path, page_num: int = 0, taille_max_px: int = TAILLE_MAX_PX) -> tuple[str, float]:
    """OCRise une page d'un PDF et renvoie (texte, durée de l'appel en secondes)."""
    png = page_to_png(pdf_path, page_num, taille_max_px)
    debut = time.perf_counter()
    texte = ocr_png(png)
    return texte, time.perf_counter() - debut
