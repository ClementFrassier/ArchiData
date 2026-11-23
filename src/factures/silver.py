"""Étape 4b — couche silver : tables propres, typées et contrôlées.

La couche silver est entièrement RECALCULÉE à partir de la couche bronze à chaque
lancement (sans refaire d'OCR) : si on corrige le parseur ou une règle de qualité,
il suffit de relancer.

Trois tables Parquet sont écrites dans le bucket silver :
- factures        : une ligne par facture acceptée, avec son statut de qualité ;
- lignes_facture  : une ligne par article facturé ;
- quarantaine     : les documents rejetés, avec le motif du rejet.

Rejetée  = document inexploitable (champ obligatoire illisible, doublon…) → quarantaine.
Signalée = facture exploitable mais suspecte (montants incohérents…) → gardée en silver
           avec statut_qualite = "signalee" et la liste des anomalies, à vérifier par un humain.
On ne corrige JAMAIS automatiquement une valeur lue.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from .bronze import lire_bronze
from .config import S3Target
from .parsing import parser_facture
from .storage import s3_client

MONTANT = pa.decimal128(12, 2)  # montants en euros : décimal exact, jamais de float
TOLERANCE = Decimal("0.01")  # 1 centime d'écart toléré (arrondis)
CHAMPS_OBLIGATOIRES = ["numero_facture", "fournisseur", "client", "date_emission",
                       "total_ht", "total_tva", "total_ttc"]

SCHEMA_FACTURES = pa.schema([
    ("numero_facture", pa.string()),
    ("fournisseur", pa.string()),
    ("fournisseur_siret", pa.string()),
    ("fournisseur_tva_intracom", pa.string()),
    ("client", pa.string()),
    ("client_siret", pa.string()),
    ("date_emission", pa.date32()),
    ("date_echeance", pa.date32()),
    ("categorie", pa.string()),
    ("statut_paiement", pa.string()),
    ("mode_paiement", pa.string()),
    ("iban", pa.string()),
    ("total_ht", MONTANT),
    ("total_tva", MONTANT),
    ("total_ttc", MONTANT),
    ("nb_lignes", pa.int32()),
    ("statut_qualite", pa.string()),  # "valide" ou "signalee"
    ("anomalies", pa.string()),  # liste des anomalies détectées, séparées par « ; »
    ("sha256", pa.string()),  # lien vers la couche bronze (traçabilité)
    ("fichier", pa.string()),
    ("methode_extraction", pa.string()),
])

SCHEMA_LIGNES = pa.schema([
    ("numero_facture", pa.string()),
    ("num_ligne", pa.int32()),
    ("designation", pa.string()),
    ("quantite", pa.int32()),
    ("prix_unitaire_ht", MONTANT),
    ("taux_tva", pa.decimal128(5, 2)),
    ("total_ht", MONTANT),
])

SCHEMA_QUARANTAINE = pa.schema([
    ("sha256", pa.string()),
    ("fichier", pa.string()),
    ("methode_extraction", pa.string()),
    ("motif", pa.string()),
    ("detail", pa.string()),
    ("date_rejet", pa.timestamp("us", tz="UTC")),
])


def derniers_documents_ok() -> list[dict]:
    """Renvoie, pour chaque document du bronze, sa dernière extraction réussie."""
    lignes = lire_bronze(S3Target.from_env("BRONZE")).to_pylist()
    par_doc = {}
    for ligne in sorted(lignes, key=lambda r: r["ingere_le"]):
        if ligne["statut"] == "ok":
            par_doc[ligne["sha256"]] = ligne
    return list(par_doc.values())


def motif_rejet(facture: dict) -> tuple[str, str] | None:
    """Renvoie (motif, détail) si la facture est inexploitable, sinon None."""
    manquants = [c for c in CHAMPS_OBLIGATOIRES if facture[c] is None]
    if manquants:
        return "champ_obligatoire_illisible", ", ".join(manquants)
    if not facture["lignes"]:
        return "aucune_ligne_de_facture", "tableau des articles introuvable"
    for i, ligne in enumerate(facture["lignes"], 1):
        if None in ligne.values():
            vides = [k for k, v in ligne.items() if v is None]
            return "ligne_illisible", f"ligne {i} : {', '.join(vides)}"
    return None


def anomalies(facture: dict) -> list[str]:
    """Liste les incohérences d'une facture exploitable (montants, dates, identifiants)."""
    pb = []
    somme_lignes = sum(l["total_ht"] for l in facture["lignes"])
    if abs(somme_lignes - facture["total_ht"]) > TOLERANCE:
        pb.append(f"somme des lignes ({somme_lignes}) ≠ total HT ({facture['total_ht']})")
    if abs(facture["total_ht"] + facture["total_tva"] - facture["total_ttc"]) > TOLERANCE:
        pb.append("total HT + TVA ≠ total TTC")
    tva_attendue = sum(l["total_ht"] * l["taux_tva"] / 100 for l in facture["lignes"])
    if abs(tva_attendue - facture["total_tva"]) > TOLERANCE:
        pb.append(f"TVA attendue {tva_attendue:.2f} ≠ TVA lue {facture['total_tva']}")
    for i, l in enumerate(facture["lignes"], 1):
        if abs(l["quantite"] * l["prix_unitaire_ht"] - l["total_ht"]) > TOLERANCE:
            pb.append(f"ligne {i} : quantité × prix unitaire ≠ total")
    if facture["date_echeance"] and facture["date_echeance"] < facture["date_emission"]:
        pb.append("date d'échéance antérieure à la date d'émission")
    for champ in ("fournisseur_siret", "client_siret"):
        if facture[champ] and not re.fullmatch(r"\d{14}", facture[champ]):
            pb.append(f"{champ} invalide (14 chiffres attendus)")
    return pb


def construire_silver() -> tuple[list[dict], list[dict], list[dict]]:
    """Parse et contrôle tous les documents du bronze ; renvoie (factures, lignes, quarantaine)."""
    factures, lignes, quarantaine = [], [], []
    numeros_vus = {}
    maintenant = datetime.now(timezone.utc)

    for doc in sorted(derniers_documents_ok(), key=lambda r: r["ingere_le"]):
        trace = {"sha256": doc["sha256"], "fichier": doc["fichier"], "methode_extraction": doc["methode"]}
        facture = parser_facture(doc["texte"])

        rejet = motif_rejet(facture)
        if rejet is None and facture["numero_facture"] in numeros_vus:
            rejet = ("doublon", f"numéro déjà présent dans {numeros_vus[facture['numero_facture']]}")
        if rejet:
            quarantaine.append({**trace, "motif": rejet[0], "detail": rejet[1], "date_rejet": maintenant})
            continue

        numeros_vus[facture["numero_facture"]] = doc["fichier"]
        pb = anomalies(facture)
        articles = facture.pop("lignes")
        factures.append({
            **facture,
            "nb_lignes": len(articles),
            "statut_qualite": "signalee" if pb else "valide",
            "anomalies": "; ".join(pb) or None,
            **trace,
        })
        for num, article in enumerate(articles, 1):
            lignes.append({"numero_facture": facture["numero_facture"], "num_ligne": num, **article})

    return factures, lignes, quarantaine


def ecrire_table(target: S3Target, nom: str, lignes: list[dict], schema: pa.Schema) -> None:
    """Écrit (en remplaçant l'ancienne version) une table Parquet dans le bucket silver."""
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(lignes, schema=schema), buffer)
    s3_client(target).put_object(Bucket=target.bucket, Key=f"{nom}/{nom}.parquet", Body=buffer.getvalue())
    print(f"→ s3://{target.bucket}/{nom}/{nom}.parquet ({len(lignes)} lignes)")


def executer() -> dict:
    """Construit la couche silver et l'écrit dans le bucket ; renvoie un résumé."""
    factures, lignes, quarantaine = construire_silver()
    target = S3Target.from_env("SILVER")
    ecrire_table(target, "factures", factures, SCHEMA_FACTURES)
    ecrire_table(target, "lignes_facture", lignes, SCHEMA_LIGNES)
    ecrire_table(target, "quarantaine", quarantaine, SCHEMA_QUARANTAINE)
    return {
        "valides": sum(f["statut_qualite"] == "valide" for f in factures),
        "signalees": [(f["numero_facture"], f["anomalies"]) for f in factures if f["statut_qualite"] == "signalee"],
        "rejets": [(q["fichier"], q["motif"], q["detail"]) for q in quarantaine],
        "lignes": len(lignes),
    }
