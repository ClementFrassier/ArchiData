"""Pipeline factures : S3 → OCR → bronze → silver → entrepôt → MCP → agent."""

from __future__ import annotations

import argparse
from collections import Counter


def _cmd_fetch(_args) -> None:
    """Commande `factures fetch` : lance le téléchargement et affiche le bilan."""
    from .fetch import fetch

    print("Récupération des factures depuis S3…")
    stats = fetch()
    print(
        f"{stats['vus']} PDF dans le bucket · {stats['telecharges']} téléchargés · "
        f"{stats['deja_presents']} déjà présents"
    )


def _cmd_detect(_args) -> None:
    """Commande `factures detect` : classe les PDF et affiche le tableau des résultats."""
    from .config import LANDING_DIR
    from .detect import detect_all

    results = detect_all()
    print(f"{'fichier':<38} {'caract.':>8}  type")
    for r in results:
        print(f"{r['fichier']:<38} {r['caracteres']:>8}  {r['type']}")
    print("\nRésumé :", dict(Counter(r["type"] for r in results)))
    print(f"Détail écrit dans {LANDING_DIR / 'detection.csv'}")


def _cmd_ocr(args) -> None:
    """Commande `factures ocr` : envoie une page au modèle, affiche le texte et le temps."""
    from pathlib import Path

    from .config import LANDING_DIR
    from .ocr import ocr_page

    pdf = Path(args.fichier)
    if not pdf.exists():
        pdf = LANDING_DIR / args.fichier
    texte, duree = ocr_page(pdf, taille_max_px=args.taille)
    print(texte)
    print(f"\n--- {pdf.name} · image {args.taille} px · {duree:.2f} s pour la page")


def _cmd_bronze(_args) -> None:
    """Commande `factures bronze` : ingère les nouveaux PDF dans la couche bronze."""
    from .bronze import ingerer

    print("Ingestion dans la couche bronze…")
    stats = ingerer()
    print(
        f"{stats['ok']} ingérés · {stats['erreurs']} en erreur (retentés au prochain lancement) · "
        f"{stats['ignores']} déjà ingérés"
    )


def _cmd_silver(_args) -> None:
    """Commande `factures silver` : reconstruit la couche silver depuis le bronze."""
    from .silver import executer

    print("Construction de la couche silver…")
    r = executer()
    print(f"\n{r['valides']} factures valides · {len(r['signalees'])} signalées · "
          f"{len(r['rejets'])} rejetées · {r['lignes']} lignes de facture")
    for numero, pb in r["signalees"]:
        print(f"  SIGNALÉE {numero} : {pb}")
    for fichier, motif, detail in r["rejets"]:
        print(f"  REJETÉE  {fichier} : {motif} ({detail})")


def _cmd_evaluer(args) -> None:
    """Commande `factures evaluer` : précision champ par champ face à la vérité terrain."""
    from .evaluation import evaluer

    evaluer(args.taille)


def _cmd_sql(args) -> None:
    """Commande `factures sql` : exécute une requête SQL sur l'entrepôt et affiche le résultat."""
    from .entrepot import connexion

    print(connexion().sql(args.requete))


def _cmd_dbeaver(_args) -> None:
    """Commande `factures dbeaver` : écrit le script SQL à coller dans DBeaver (avec les clés)."""
    from .config import DATA_DIR
    from .entrepot import script_initialisation

    chemin = DATA_DIR / "dbeaver_init.sql"
    chemin.write_text(";\n".join(script_initialisation()) + ";\n")
    print(f"Script écrit dans {chemin}")
    print("Il contient vos clés S3 : ne le partagez pas (le dossier data/ est ignoré par Git).")


def _cmd_pipeline(_args) -> None:
    """Commande `factures pipeline` : récupération → bronze → silver, en une seule commande."""
    import sys
    import time

    debut = time.perf_counter()
    _cmd_fetch(None)
    print()
    from .bronze import ingerer

    print("Ingestion dans la couche bronze…")
    stats = ingerer()
    print(f"{stats['ok']} ingérés · {stats['erreurs']} en erreur · {stats['ignores']} déjà ingérés\n")
    _cmd_silver(None)
    print(f"\nPipeline terminé en {time.perf_counter() - debut:.1f} s")
    # Code de sortie non nul s'il reste des documents en erreur : le planificateur peut alerter
    sys.exit(1 if stats["erreurs"] else 0)


def main() -> None:
    """Point d'entrée : lit la sous-commande tapée (fetch, detect…) et l'exécute."""
    parser = argparse.ArgumentParser(prog="factures", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="Télécharger les nouvelles factures du bucket S3").set_defaults(func=_cmd_fetch)
    sub.add_parser("detect", help="Classer chaque PDF : texte ou image (OCR)").set_defaults(func=_cmd_detect)
    p_ocr = sub.add_parser("ocr", help="Envoyer une page au modèle d'OCR et afficher le résultat")
    p_ocr.add_argument("fichier", help="PDF à OCRiser (chemin ou nom dans data/landing/)")
    p_ocr.add_argument("--taille", type=int, default=1540, help="côté le plus long de l'image en px")
    p_ocr.set_defaults(func=_cmd_ocr)
    sub.add_parser("bronze", help="Ingérer les nouveaux PDF dans la couche bronze").set_defaults(func=_cmd_bronze)
    sub.add_parser("silver", help="Construire la couche silver (tables typées + quarantaine)").set_defaults(func=_cmd_silver)
    sub.add_parser("pipeline", help="Tout enchaîner : récupération → bronze → silver").set_defaults(func=_cmd_pipeline)
    p_eval = sub.add_parser("evaluer", help="Mesurer la qualité de l'OCR face à la vérité terrain")
    p_eval.add_argument("--taille", type=int, default=None,
                        help="ré-OCRiser l'échantillon à cette taille (px) au lieu de lire le silver")
    p_eval.set_defaults(func=_cmd_evaluer)
    p_sql = sub.add_parser("sql", help="Exécuter une requête SQL sur l'entrepôt (bronze / silver)")
    p_sql.add_argument("requete", help='ex. "SELECT count(*) FROM silver.factures"')
    p_sql.set_defaults(func=_cmd_sql)
    sub.add_parser("dbeaver", help="Générer le script d'initialisation pour DBeaver").set_defaults(func=_cmd_dbeaver)
    args = parser.parse_args()
    args.func(args)
