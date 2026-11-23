"""Étape 4 : lecture des champs d'une facture à partir de son texte (natif ou OCR)."""

import re
from datetime import date, datetime
from decimal import Decimal

ENTETE_TABLEAU = ["Désignation", "Qté", "Prix unitaire HT", "TVA", "Total HT"]
MONTANT_RE = re.compile(r"^-?\d{1,3}(?: \d{3})*,\d{2} ?€?$")


def normaliser(texte: str) -> list[str]:
    """Retire la mise en forme Markdown et HTML et renvoie la liste des lignes utiles."""
    texte = texte.replace(" ", " ").replace(" ", " ")
    texte = re.sub(r"</t[dh]>", "\n", texte)
    texte = re.sub(r"<[^>]+>", "", texte)
    texte = texte.replace("**", "")
    lignes = []
    for ligne in texte.splitlines():
        ligne = re.sub(r"^#+\s*", "", ligne)
        ligne = re.sub(r"\s+", " ", ligne).strip()
        if ligne and ligne != "---":
            lignes.append(ligne)
    return lignes


def lire_montant(valeur: str | None) -> Decimal | None:
    """Convertit « 1 541,05 € » en Decimal('1541.05'), ou None si le format est invalide."""
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not MONTANT_RE.match(valeur):
        return None
    return Decimal(valeur.replace("€", "").replace(" ", "").replace(",", "."))


def lire_date(valeur: str | None) -> date | None:
    """Convertit « 29/04/2025 » en date, ou None si la date est absente ou invalide."""
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
    """Lit un total, avec le montant sur la même ligne ou sur la ligne suivante."""
    trouve = None
    for i, ligne in enumerate(lignes):
        if not ligne.startswith(libelle):
            continue
        reste = ligne[len(libelle):].strip()
        suivante = lignes[i + 1] if i + 1 < len(lignes) else None
        montant = lire_montant(reste) if reste else lire_montant(suivante)
        if montant is not None:
            trouve = montant
    return trouve


def lire_lignes_facture(lignes: list[str]) -> list[dict]:
    """Lit le tableau des lignes de facture (groupes de 5 valeurs après l'en-tête)."""
    for debut in range(len(lignes) - len(ENTETE_TABLEAU)):
        if lignes[debut:debut + len(ENTETE_TABLEAU)] == ENTETE_TABLEAU:
            break
    else:
        return []

    resultat = []
    i = debut + len(ENTETE_TABLEAU)
    while i + 5 <= len(lignes) and not lignes[i].startswith(("Total ", "TVA ", "Mode de paiement")):
        designation, qte, pu, tva, total = lignes[i:i + 5]
        taux = None
        if re.fullmatch(r"\d+(?:,\d+)? ?%", tva):
            taux = Decimal(tva.replace("%", "").replace(",", ".").strip())
        resultat.append(
            {
                "designation": designation,
                "quantite": int(qte) if qte.isdigit() else None,
                "prix_unitaire_ht": lire_montant(pu),
                "taux_tva": taux,
                "total_ht": lire_montant(total),
            }
        )
        i += 5
    return resultat


def lire_fournisseur(lignes: list[str]) -> str | None:
    """Renvoie le nom du fournisseur : la première ligne qui n'est pas le titre « FACTURE »."""
    for ligne in lignes:
        if ligne.upper() != "FACTURE":
            return ligne
    return None


def parser_facture(texte: str) -> dict:
    """Extrait tous les champs d'une facture à partir de son texte brut."""
    lignes = normaliser(texte)
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
        "statut_paiement": lire_champ(lignes, "Statut"),
        "mode_paiement": lire_champ(lignes, "Mode de paiement"),
        "iban": lire_champ(lignes, "IBAN"),
        "total_ht": lire_total(lignes, "Total HT"),
        "total_tva": lire_total(lignes, "Total TVA"),
        "total_ttc": lire_total(lignes, "Total TTC"),
        "lignes": lire_lignes_facture(lignes),
    }
