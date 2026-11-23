"""Étape 4a — transformer le texte brut d'une facture en champs structurés.

Le texte d'un PDF natif et celui de l'OCR n'ont pas la même forme :
- natif : une valeur par ligne (libellé puis montant sur la ligne suivante) ;
- OCR   : Markdown (gras **, titres #, séparateurs ---) et tableaux en HTML.

Stratégie : on NORMALISE d'abord le texte (on retire la mise en forme et on met
une cellule de tableau par ligne) pour obtenir la même forme dans les deux cas,
puis un seul parseur lit les champs.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

ENTETE_TABLEAU = ["Désignation", "Qté", "Prix unitaire HT", "TVA", "Total HT"]
MONTANT_RE = re.compile(r"^-?\d{1,3}(?: \d{3})*,\d{2} ?€?$")


def normaliser(texte: str) -> list[str]:
    """Retire la mise en forme Markdown/HTML et renvoie la liste des lignes utiles."""
    texte = texte.replace(" ", " ").replace(" ", " ")  # espaces insécables
    texte = re.sub(r"</t[dh]>", "\n", texte)  # chaque cellule de tableau → une ligne
    texte = re.sub(r"<[^>]+>", "", texte)  # supprime les autres balises HTML
    texte = texte.replace("**", "")  # supprime le gras Markdown
    lignes = []
    for ligne in texte.splitlines():
        ligne = re.sub(r"^#+\s*", "", ligne)  # supprime les titres Markdown
        ligne = re.sub(r"\s+", " ", ligne).strip()
        if ligne and ligne != "---":
            lignes.append(ligne)
    return lignes


def lire_montant(valeur: str | None) -> Decimal | None:
    """Convertit « 1 541,05 € » en Decimal('1541.05') ; None si le format est invalide."""
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not MONTANT_RE.match(valeur):
        return None
    return Decimal(valeur.replace("€", "").replace(" ", "").replace(",", "."))


def lire_date(valeur: str | None) -> date | None:
    """Convertit « 29/04/2025 » en date ; None si la date est absente ou invalide."""
    try:
        return datetime.strptime(valeur.strip(), "%d/%m/%Y").date()
    except (AttributeError, ValueError):
        return None


def lire_champ(lignes: list[str], libelle: str) -> str | None:
    """Cherche une ligne « Libellé : valeur » et renvoie la valeur."""
    motif = re.compile(rf"^{re.escape(libelle)}\s*:\s*(.+)$")
    for ligne in lignes:
        m = motif.match(ligne)
        if m:
            return m.group(1).strip()
    return None


def lire_total(lignes: list[str], libelle: str) -> Decimal | None:
    """Lit un total (« Total HT 30 811,50 € » sur une ligne, ou montant sur la ligne suivante)."""
    trouve = None
    for i, ligne in enumerate(lignes):
        if not ligne.startswith(libelle):
            continue
        reste = ligne[len(libelle):].strip()
        suivante = lignes[i + 1] if i + 1 < len(lignes) else None
        montant = lire_montant(reste) if reste else lire_montant(suivante)
        if montant is not None:
            trouve = montant  # on garde la dernière occurrence (l'en-tête du tableau n'a pas de montant)
    return trouve


def lire_lignes_facture(lignes: list[str]) -> list[dict]:
    """Lit le tableau des lignes de facture (groupes de 5 valeurs après l'en-tête)."""
    for debut in range(len(lignes) - len(ENTETE_TABLEAU)):
        if lignes[debut:debut + len(ENTETE_TABLEAU)] == ENTETE_TABLEAU:
            break
    else:
        return []  # en-tête introuvable

    resultat = []
    i = debut + len(ENTETE_TABLEAU)
    while i + 5 <= len(lignes) and not lignes[i].startswith(("Total ", "TVA ", "Mode de paiement")):
        designation, qte, pu, tva, total = lignes[i:i + 5]
        resultat.append(
            {
                "designation": designation,
                "quantite": int(qte) if qte.isdigit() else None,
                "prix_unitaire_ht": lire_montant(pu),
                "taux_tva": Decimal(tva.replace("%", "").replace(",", ".").strip())
                if re.fullmatch(r"\d+(?:,\d+)? ?%", tva) else None,
                "total_ht": lire_montant(total),
            }
        )
        i += 5
    return resultat


def lire_fournisseur(lignes: list[str]) -> str | None:
    """Le nom du fournisseur est la première ligne qui n'est pas le titre « FACTURE »."""
    for ligne in lignes:
        if ligne.upper() != "FACTURE":
            return ligne
    return None


def parser_facture(texte: str) -> dict:
    """Extrait tous les champs d'une facture à partir de son texte brut."""
    lignes = normaliser(texte)
    statut = lire_champ(lignes, "Statut")
    return {
        "numero_facture": lire_champ(lignes, "Numéro de facture"),
        "fournisseur": lire_fournisseur(lignes),
        "fournisseur_siret": lire_champ(lignes, "SIRET"),
        "fournisseur_tva_intracom": lire_champ(lignes, "TVA intracom."),
        "client": lire_champ(lignes, "Client"),
        "client_siret": lire_champ(lignes, "SIRET client"),
        "date_emission": lire_date(lire_champ(lignes, "Date d'émission")),
        "date_echeance": lire_date(lire_champ(lignes, "Date d'échéance")),
        "categorie": lire_champ(lignes, "Catégorie"),
        "statut_paiement": statut,
        "mode_paiement": lire_champ(lignes, "Mode de paiement"),
        "iban": lire_champ(lignes, "IBAN"),
        "total_ht": lire_total(lignes, "Total HT"),
        "total_tva": lire_total(lignes, "Total TVA"),
        "total_ttc": lire_total(lignes, "Total TTC"),
        "lignes": lire_lignes_facture(lignes),
    }
