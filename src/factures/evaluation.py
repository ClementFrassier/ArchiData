"""Étape 5 : mesure de la qualité de l'OCR par rapport à la vérité terrain."""

import csv
import io

import pyarrow.parquet as pq

from .config import DATA_DIR, LANDING_DIR, PROJECT_ROOT, S3Target
from .ocr import ocr_page
from .parsing import parser_facture
from .storage import s3_client

VERITE = PROJECT_ROOT / "evaluation" / "verite_terrain.csv"
CHAMPS = ["numero_facture", "fournisseur", "fournisseur_siret", "fournisseur_tva_intracom",
          "client", "client_siret", "date_emission", "date_echeance", "categorie",
          "statut_paiement", "mode_paiement", "iban", "total_ht", "total_tva", "total_ttc", "nb_lignes"]


def charger_verite() -> list[dict]:
    """Lit le fichier de vérité terrain (une ligne par facture de l'échantillon)."""
    with VERITE.open() as f:
        return list(csv.DictReader(f))


def depuis_silver() -> dict[str, dict]:
    """Lit la table factures de la couche silver, indexée par nom de fichier."""
    t = S3Target.from_env("SILVER")
    data = s3_client(t).get_object(Bucket=t.bucket, Key="factures/factures.parquet")["Body"].read()
    return {r["fichier"]: r for r in pq.read_table(io.BytesIO(data)).to_pylist()}


def depuis_ocr(fichiers: list[str], taille: int) -> dict[str, dict]:
    """OCRise les fichiers à la taille demandée (résultats mis en cache) et les parse."""
    cache = DATA_DIR / "evaluation" / f"ocr_{taille}px"
    cache.mkdir(parents=True, exist_ok=True)
    resultats = {}
    for fichier in fichiers:
        chemin = cache / f"{fichier}.md"
        if not chemin.exists():
            texte, duree = ocr_page(LANDING_DIR / fichier, 0, taille)
            chemin.write_text(texte)
            print(f"  OCR {taille}px {fichier} ({duree:.1f} s)")
        facture = parser_facture(chemin.read_text())
        facture["nb_lignes"] = len(facture.pop("lignes"))
        resultats[fichier] = facture
    return resultats


def en_texte(valeur) -> str:
    """Met une valeur (date, décimal, entier, None) sous forme de texte comparable."""
    return "" if valeur is None else str(valeur).strip()


def evaluer(taille: int | None = None) -> None:
    """Compare les valeurs extraites à la vérité terrain et affiche la précision par champ."""
    verite = charger_verite()
    fichiers = [v["fichier"] for v in verite]
    extrait = depuis_silver() if taille is None else depuis_ocr(fichiers, taille)
    source = "silver (1540 px)" if taille is None else f"OCR {taille} px"

    resultats, erreurs = [], []
    for champ in CHAMPS:
        corrects = 0
        for v in verite:
            lu = en_texte(extrait.get(v["fichier"], {}).get(champ))
            if lu == v[champ]:
                corrects += 1
            else:
                erreurs.append((v["fichier"], champ, v[champ], lu))
        resultats.append({"champ": champ, "corrects": corrects, "total": len(verite),
                          "precision": round(100 * corrects / len(verite), 1)})

    sortie = PROJECT_ROOT / "evaluation" / f"resultats_{'silver' if taille is None else f'{taille}px'}.csv"
    with sortie.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["champ", "corrects", "total", "precision"])
        writer.writeheader()
        writer.writerows(resultats)

    print(f"\nSource : {source}, {len(verite)} factures scannées\n")
    print(f"{'champ':<26} {'corrects':>9} {'précision':>10}")
    for r in resultats:
        print(f"{r['champ']:<26} {r['corrects']:>4}/{r['total']:<4} {r['precision']:>9.1f}%")
    total = sum(r["corrects"] for r in resultats)
    nb_valeurs = len(verite) * len(CHAMPS)
    print(f"{'GLOBAL':<26} {total:>4}/{nb_valeurs:<4} {100 * total / nb_valeurs:>9.1f}%")
    for fichier, champ, attendu, lu in erreurs:
        print(f"  ERREUR {fichier}, {champ} : attendu « {attendu} », lu « {lu} »")
    print(f"\nRésultats écrits dans {sortie.relative_to(PROJECT_ROOT)}")
