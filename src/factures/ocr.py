"""Étape 2 : appel du modèle d'OCR LightOnOCR-3 (API compatible OpenAI)."""

import base64
import time
from pathlib import Path

import pymupdf
from openai import OpenAI

from .config import variable

TAILLE_MAX_PX = 1540


def page_to_png(pdf_path: Path, page_num: int = 0, taille_max_px: int = TAILLE_MAX_PX) -> bytes:
    """Convertit une page du PDF en image PNG dont le plus grand côté fait taille_max_px."""
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_num]
        zoom = taille_max_px / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")


def ocr_png(png: bytes) -> str:
    """Envoie une image PNG au modèle d'OCR et renvoie le texte reconnu (Markdown)."""
    client = OpenAI(base_url=variable("OCR_ENDPOINT"), api_key=variable("OCR_API_KEY"),
                    timeout=120, max_retries=2)
    image_b64 = base64.b64encode(png).decode("utf-8")
    response = client.chat.completions.create(
        model=variable("OCR_MODEL"),
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
    """OCRise une page d'un PDF et renvoie le texte et la durée de l'appel en secondes."""
    png = page_to_png(pdf_path, page_num, taille_max_px)
    debut = time.perf_counter()
    texte = ocr_png(png)
    return texte, time.perf_counter() - debut
