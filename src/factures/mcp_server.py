"""Étape 7 — serveur MCP : donner à une IA un accès en LECTURE SEULE à l'entrepôt.

Outils exposés :
- lister_factures   : liste filtrée (fournisseur, client, statut, catégorie, période…)
- totaux_factures   : nombre et totaux HT / TVA / TTC avec les mêmes filtres
- detail_facture    : une facture et ses lignes
- schema_donnees    : tables, colonnes et types disponibles
- requete_sql       : requête SQL libre, en lecture seule

Chaque réponse contient la clé "sql" : les requêtes réellement exécutées.

Sécurité (les arguments viennent d'un LLM, donc d'une source non fiable) :
1. les filtres sont passés en PARAMÈTRES ($1, $2…) et jamais collés dans le texte SQL ;
2. la requête libre est analysée par DuckDB : une seule instruction, de type SELECT ;
3. DuckDB est verrouillé : aucun accès aux fichiers locaux ni à d'autres URL que nos
   buckets, et la configuration ne peut plus être modifiée.
"""

from __future__ import annotations

import datetime
import decimal
import re

import duckdb
from mcp.server.mcpserver import MCPServer

from .config import S3Target
from .entrepot import connexion

LIMITE_MAX = 200  # nombre maximum de lignes renvoyées au LLM
MOTS_INTERDITS = re.compile(r"duckdb_secrets|which_secret|duckdb_settings", re.IGNORECASE)

mcp = MCPServer("factures")
_con: duckdb.DuckDBPyConnection | None = None


def base() -> duckdb.DuckDBPyConnection:
    """Ouvre (une seule fois) la connexion à l'entrepôt, puis la verrouille."""
    global _con
    if _con is None:
        _con = connexion()
        buckets = [S3Target.from_env(p).bucket for p in ("BRONZE", "SILVER")]
        liste = ", ".join(f"'s3://{b}/'" for b in buckets)
        _con.execute(f"SET allowed_directories = [{liste}]")
        _con.execute("SET enable_external_access = false")  # plus de fichiers locaux ni d'autres URL
        _con.execute("SET lock_configuration = true")  # ces réglages ne peuvent plus être changés
    return _con


def en_json(valeur):
    """Convertit les décimaux et les dates en texte pour la réponse JSON."""
    if isinstance(valeur, decimal.Decimal):
        return str(valeur)  # texte exact, pas de float
    if isinstance(valeur, (datetime.date, datetime.datetime)):
        return valeur.isoformat()
    return valeur


def executer(sql: str, params: list | None = None, limite: int = LIMITE_MAX) -> list[dict]:
    """Exécute une requête paramétrée et renvoie les lignes sous forme de dictionnaires."""
    cur = base().cursor()  # un curseur par appel : plusieurs outils peuvent tourner en parallèle
    cur.execute(sql, params or [])
    colonnes = [d[0] for d in cur.description]
    return [{c: en_json(v) for c, v in zip(colonnes, ligne)} for ligne in cur.fetchmany(limite)]


def construire_filtres(fournisseur, client, statut_paiement, statut_qualite,
                       categorie, date_debut, date_fin) -> tuple[str, list]:
    """Construit la clause WHERE et la liste des paramètres à partir des filtres fournis."""
    conditions, params = [], []
    for colonne, valeur, partiel in [
        ("fournisseur", fournisseur, True), ("client", client, True),
        ("statut_paiement", statut_paiement, False), ("statut_qualite", statut_qualite, False),
        ("categorie", categorie, False),
    ]:
        if valeur:
            params.append(f"%{valeur}%" if partiel else valeur)
            operateur = "ILIKE" if partiel else "="
            conditions.append(f"{colonne} {operateur} ${len(params)}")
    if date_debut:
        params.append(date_debut)
        conditions.append(f"date_emission >= CAST(${len(params)} AS DATE)")
    if date_fin:
        params.append(date_fin)
        conditions.append(f"date_emission <= CAST(${len(params)} AS DATE)")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def erreur(message: str, sql: str | None, conseil: str) -> dict:
    """Formate une erreur que le LLM peut comprendre pour corriger son appel."""
    return {"erreur": message, "sql": [sql] if sql else [], "conseil": conseil}


@mcp.tool()
def lister_factures(
    fournisseur: str | None = None,
    client: str | None = None,
    statut_paiement: str | None = None,
    statut_qualite: str | None = None,
    categorie: str | None = None,
    date_debut: str | None = None,
    date_fin: str | None = None,
    limite: int = 50,
) -> dict:
    """Liste les factures (une ligne par facture) avec des filtres optionnels.

    Utiliser pour afficher ou parcourir des factures. Pour un nombre ou une somme,
    préférer totaux_factures.
    - fournisseur, client : recherche partielle, insensible à la casse (ex. "aubry")
    - statut_paiement : "Payée", "En retard" ou "En attente"
    - statut_qualite : "valide" ou "signalee" (montants incohérents)
    - categorie : "Informatique", "Fournitures", "Maintenance" ou "Conseil"
    - date_debut, date_fin : période sur la date d'émission, format AAAA-MM-JJ
    - limite : nombre maximum de factures renvoyées (1 à 200)
    Les montants sont en euros, sous forme de texte exact (ex. "36973.80").
    """
    where, params = construire_filtres(fournisseur, client, statut_paiement, statut_qualite,
                                       categorie, date_debut, date_fin)
    sql = ("SELECT numero_facture, fournisseur, client, date_emission, date_echeance, categorie, "
           "statut_paiement, total_ht, total_tva, total_ttc, statut_qualite "
           f"FROM silver.factures{where} ORDER BY date_emission LIMIT {max(1, min(limite, LIMITE_MAX))}")
    try:
        lignes = executer(sql, params)
    except duckdb.Error as e:
        return erreur(str(e), sql, "Vérifiez le format des filtres (dates en AAAA-MM-JJ).")
    return {"factures": lignes, "nombre_renvoye": len(lignes), "sql": [sql], "parametres": params}


@mcp.tool()
def totaux_factures(
    fournisseur: str | None = None,
    client: str | None = None,
    statut_paiement: str | None = None,
    statut_qualite: str | None = None,
    categorie: str | None = None,
    date_debut: str | None = None,
    date_fin: str | None = None,
) -> dict:
    """Calcule le NOMBRE de factures et la SOMME des montants HT, TVA et TTC (en euros).

    Utiliser pour toute question « combien », « quel montant », « quel total ».
    Mêmes filtres que lister_factures :
    - fournisseur, client : recherche partielle, insensible à la casse
    - statut_paiement : "Payée", "En retard" ou "En attente"
    - statut_qualite : "valide" ou "signalee"
    - categorie : "Informatique", "Fournitures", "Maintenance" ou "Conseil"
    - date_debut, date_fin : période sur la date d'émission, format AAAA-MM-JJ
      (ex. toute l'année 2025 : date_debut="2025-01-01", date_fin="2025-12-31")
    """
    where, params = construire_filtres(fournisseur, client, statut_paiement, statut_qualite,
                                       categorie, date_debut, date_fin)
    sql = ("SELECT count(*) AS nombre_factures, coalesce(sum(total_ht), 0) AS total_ht, "
           "coalesce(sum(total_tva), 0) AS total_tva, coalesce(sum(total_ttc), 0) AS total_ttc "
           f"FROM silver.factures{where}")
    try:
        resultat = executer(sql, params)[0]
    except duckdb.Error as e:
        return erreur(str(e), sql, "Vérifiez le format des filtres (dates en AAAA-MM-JJ).")
    return {**resultat, "sql": [sql], "parametres": params}


@mcp.tool()
def detail_facture(numero_facture: str) -> dict:
    """Renvoie le détail complet d'UNE facture (en-tête, statut de qualité) et ses lignes d'articles.

    - numero_facture : numéro exact, par exemple "A-2025-0001"
    """
    sql_facture = "SELECT * FROM silver.factures WHERE numero_facture = $1"
    sql_lignes = ("SELECT num_ligne, designation, quantite, prix_unitaire_ht, taux_tva, total_ht "
                  "FROM silver.lignes_facture WHERE numero_facture = $1 ORDER BY num_ligne")
    facture = executer(sql_facture, [numero_facture])
    if not facture:
        return erreur(f"Aucune facture avec le numéro {numero_facture!r}.", sql_facture,
                      "Utilisez lister_factures pour retrouver le numéro exact (format ex. A-2025-0001).")
    lignes = executer(sql_lignes, [numero_facture])
    return {"facture": facture[0], "lignes": lignes, "sql": [sql_facture, sql_lignes],
            "parametres": [numero_facture]}


@mcp.tool()
def schema_donnees() -> dict:
    """Décrit les tables disponibles, leurs colonnes et leurs types.

    À appeler avant d'écrire une requête avec requete_sql.
    Tables : silver.factures (une ligne par facture), silver.lignes_facture (articles,
    jointure sur numero_facture), silver.quarantaine (documents rejetés),
    bronze.documents (texte brut extrait et traçabilité).
    """
    sql = ("SELECT table_schema || '.' || table_name AS table_nom, column_name AS colonne, "
           "data_type AS type FROM information_schema.columns "
           "WHERE table_schema IN ('bronze', 'silver') ORDER BY table_schema, table_name, ordinal_position")
    tables: dict[str, list] = {}
    for ligne in executer(sql, limite=1000):
        tables.setdefault(ligne["table_nom"], []).append(f"{ligne['colonne']} ({ligne['type']})")
    return {"tables": tables, "sql": [sql]}


@mcp.tool()
def requete_sql(sql: str) -> dict:
    """Exécute une requête SQL LIBRE en LECTURE SEULE (dialecte DuckDB) et renvoie au plus 200 lignes.

    À utiliser seulement si les autres outils ne suffisent pas (regroupements, jointures…).
    Une seule instruction SELECT (ou WITH … SELECT) est acceptée ; toute modification est refusée.
    Appeler schema_donnees d'abord pour connaître les tables et les colonnes.
    Exemple : SELECT categorie, sum(total_ttc) FROM silver.factures GROUP BY categorie
    """
    try:
        instructions = base().extract_statements(sql)
    except duckdb.Error as e:
        return erreur(f"Requête SQL invalide : {e}", sql, "Corrigez la syntaxe (dialecte DuckDB).")
    if len(instructions) != 1:
        return erreur("Une seule instruction SQL est autorisée.", sql, "Retirez les « ; » en trop.")
    if instructions[0].type != duckdb.StatementType.SELECT:
        return erreur(f"Instruction refusée ({instructions[0].type.name}) : accès en lecture seule.", sql,
                      "Seules les requêtes SELECT sont autorisées.")
    if MOTS_INTERDITS.search(sql):
        return erreur("Accès aux informations internes de DuckDB refusé.", sql,
                      "Interrogez uniquement les tables bronze.* et silver.*.")
    try:
        lignes = executer(sql)
    except duckdb.Error as e:
        return erreur(str(e), sql, "Vérifiez les noms de tables et de colonnes avec schema_donnees.")
    return {"lignes": lignes, "nombre_renvoye": len(lignes), "tronque": len(lignes) == LIMITE_MAX, "sql": [sql]}


def main() -> None:
    """Démarre le serveur MCP (communication par l'entrée/sortie standard)."""
    mcp.run()
