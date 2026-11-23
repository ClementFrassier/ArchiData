"""Test du serveur MCP (lancement : uv run python tests/test_mcp.py)."""

import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from factures.config import S3Target
from factures.entrepot import connexion
from factures.storage import list_objects

RESULTATS = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    """Enregistre et affiche le résultat d'une vérification."""
    RESULTATS.append(condition)
    print(f"  {'OK   ' if condition else 'ÉCHEC'} {nom}" + (f" : {detail}" if detail and not condition else ""))


async def appeler(session: ClientSession, outil: str, **arguments) -> dict:
    """Appelle un outil du serveur MCP et renvoie sa réponse décodée."""
    reponse = await session.call_tool(outil, arguments)
    return json.loads(reponse.content[0].text)


async def tester() -> None:
    """Démarre le serveur MCP, appelle chaque outil et vérifie les réponses."""
    attendu = connexion()
    fichiers_avant = sorted(o["Key"] for o in list_objects(S3Target.from_env("SILVER")))

    serveur = StdioServerParameters(command="uv", args=["run", "factures-mcp"])
    async with stdio_client(serveur) as (lecture, ecriture):
        async with ClientSession(lecture, ecriture) as session:
            await session.initialize()

            print("Outils exposés")
            outils = {o.name for o in (await session.list_tools()).tools}
            verifier("5 outils présents", outils == {"lister_factures", "totaux_factures", "detail_facture",
                                                     "schema_donnees", "requete_sql"}, str(outils))

            print("lister_factures")
            r = await appeler(session, "lister_factures", fournisseur="aubry", statut_paiement="En retard")
            n = attendu.execute("SELECT count(*) FROM silver.factures WHERE fournisseur ILIKE '%aubry%' "
                            "AND statut_paiement = 'En retard'").fetchone()[0]
            verifier("filtre fournisseur + statut", r["nombre_renvoye"] == n, f"{r['nombre_renvoye']} != {n}")
            verifier("SQL exécuté renvoyé", "silver.factures" in r["sql"][0])
            verifier("filtres passés en paramètres", "aubry" not in r["sql"][0] and "%aubry%" in r["parametres"])
            r = await appeler(session, "lister_factures", date_debut="2026-01-01", date_fin="2026-03-31")
            n = attendu.execute("SELECT count(*) FROM silver.factures WHERE date_emission "
                            "BETWEEN '2026-01-01' AND '2026-03-31'").fetchone()[0]
            verifier("filtre période", r["nombre_renvoye"] == n, f"{r['nombre_renvoye']} != {n}")

            print("totaux_factures")
            r = await appeler(session, "totaux_factures", statut_paiement="En retard",
                              date_debut="2025-01-01", date_fin="2025-12-31")
            nb, ttc = attendu.execute("SELECT count(*), sum(total_ttc) FROM silver.factures WHERE statut_paiement = "
                                  "'En retard' AND year(date_emission) = 2025").fetchone()
            verifier("nombre (en retard, 2025)", r["nombre_factures"] == nb, f"{r['nombre_factures']} != {nb}")
            verifier("total TTC exact", r["total_ttc"] == str(ttc), f"{r['total_ttc']} != {ttc}")
            r = await appeler(session, "totaux_factures", client="client_qui_n_existe_pas")
            verifier("aucun résultat, total à 0", r["nombre_factures"] == 0 and r["total_ttc"] == "0.00", str(r))

            print("detail_facture")
            r = await appeler(session, "detail_facture", numero_facture="A-2025-0001")
            n = attendu.execute("SELECT count(*) FROM silver.lignes_facture WHERE numero_facture = 'A-2025-0001'").fetchone()[0]
            verifier("en-tête de la facture", r["facture"]["numero_facture"] == "A-2025-0001")
            verifier("lignes de la facture", len(r["lignes"]) == n, f"{len(r['lignes'])} != {n}")
            verifier("2 requêtes SQL renvoyées", len(r["sql"]) == 2)
            r = await appeler(session, "detail_facture", numero_facture="ZZ-0000")
            verifier("numéro inconnu, erreur avec conseil", "erreur" in r and "conseil" in r)

            print("schema_donnees")
            r = await appeler(session, "schema_donnees")
            verifier("4 tables décrites", set(r["tables"]) == {"bronze.documents", "silver.factures",
                                                               "silver.lignes_facture", "silver.quarantaine"})
            verifier("colonnes typées", any("total_ttc (DECIMAL(12,2))" == c for c in r["tables"]["silver.factures"]))

            print("requete_sql (lecture)")
            r = await appeler(session, "requete_sql",
                              sql="SELECT categorie, count(*) AS n FROM silver.factures GROUP BY categorie ORDER BY n DESC")
            verifier("SELECT autorisé", r.get("lignes") and r["lignes"][0]["categorie"] == "Informatique", str(r)[:200])
            r = await appeler(session, "requete_sql", sql="SELECT colonne_inexistante FROM silver.factures")
            verifier("erreur SQL, message et conseil", "erreur" in r and "conseil" in r)

            print("requete_sql (tentatives de modification : toutes doivent être refusées)")
            attaques = {
                "DELETE": "DELETE FROM silver.factures",
                "DROP VIEW": "DROP VIEW silver.factures",
                "CREATE TABLE": "CREATE TABLE pirate AS SELECT 1",
                "COPY vers S3": "COPY (SELECT 1) TO 's3://default-silver/pirate.parquet'",
                "2 instructions": "SELECT 1; DROP VIEW silver.factures",
                "ATTACH": "ATTACH 'pirate.duckdb'",
                "SET": "SET enable_external_access = true",
                "lire le .env": "SELECT * FROM read_csv('.env')",
                "lire les secrets": "SELECT * FROM duckdb_secrets()",
            }
            for nom, sql in attaques.items():
                r = await appeler(session, "requete_sql", sql=sql)
                verifier(f"refusé : {nom}", "erreur" in r, str(r)[:150])

            r = await appeler(session, "totaux_factures")
            verifier("données intactes après les attaques", r["nombre_factures"] == 100)

    fichiers_apres = sorted(o["Key"] for o in list_objects(S3Target.from_env("SILVER")))
    verifier("aucun fichier ajouté ou supprimé dans le bucket silver", fichiers_avant == fichiers_apres)


if __name__ == "__main__":
    asyncio.run(tester())
    print(f"\n{sum(RESULTATS)}/{len(RESULTATS)} vérifications réussies")
    raise SystemExit(0 if all(RESULTATS) else 1)
