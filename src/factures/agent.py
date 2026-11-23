"""Étape 8 : agent qui répond aux questions sur les factures avec les outils MCP."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

from .config import variable

MAX_ETAPES = 8

CONSIGNES = """Tu es un assistant qui répond en français aux questions sur les factures d'une entreprise.
Règles :
- Tu obtiens les données uniquement en appelant les outils fournis. Tu n'as aucune autre source.
- N'invente jamais un chiffre, un nom ou une date : chaque valeur de ta réponse doit venir d'un résultat d'outil.
- Pour un nombre ou un montant total, utilise totaux_factures et ne fais pas les additions toi-même.
- Utilise seulement les filtres demandés par l'utilisateur, n'en ajoute aucun de toi-même
  (pas de catégorie, de fournisseur ou de client s'ils ne sont pas dans la question).
- Si un outil renvoie une erreur, lis le conseil et corrige ton appel.
- Si les outils ne permettent pas de répondre, dis-le clairement.
- Les montants sont en euros, écris-les au format français (ex. 106 886,43 €).
- Réponds de façon courte et précise, et indique les filtres utilisés (période, statut)."""


def config_llm() -> tuple[AsyncOpenAI, str]:
    """Crée le client du LLM à partir des variables LLM_BASE_URL, LLM_API_KEY et LLM_MODEL."""
    client = AsyncOpenAI(
        base_url=variable("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY") or "aucune",
        timeout=300,
    )
    return client, variable("LLM_MODEL")


def outil_mcp_vers_openai(outil) -> dict:
    """Traduit la description d'un outil MCP au format attendu par l'API OpenAI."""
    return {
        "type": "function",
        "function": {"name": outil.name, "description": outil.description or "", "parameters": outil.input_schema},
    }


def afficher_resultat(resultat: dict) -> None:
    """Affiche le SQL exécuté et un aperçu du résultat d'un outil."""
    for requete in resultat.get("sql", []):
        print(f"   SQL      : {requete}")
    if resultat.get("parametres"):
        print(f"   params   : {resultat['parametres']}")
    apercu = {k: v for k, v in resultat.items() if k not in ("sql", "parametres")}
    texte = json.dumps(apercu, ensure_ascii=False)
    if len(texte) > 400:
        texte = texte[:400] + " ..."
    print(f"   résultat : {texte}")


async def repondre(question: str, messages: list, session: ClientSession, outils: list,
                   client: AsyncOpenAI, modele: str) -> str:
    """Fait répondre le LLM à une question, en exécutant les outils MCP qu'il demande."""
    messages.append({"role": "user", "content": question})
    for _ in range(MAX_ETAPES):
        reponse = await client.chat.completions.create(model=modele, messages=messages,
                                                       tools=outils, temperature=0)
        message = reponse.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            return message.content or ""

        for appel in message.tool_calls:
            nom = appel.function.name
            try:
                arguments = json.loads(appel.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            print(f"\n   outil    : {nom}({json.dumps(arguments, ensure_ascii=False)})")
            try:
                retour = await session.call_tool(nom, arguments)
                texte = retour.content[0].text if retour.content else "{}"
            except Exception as e:
                texte = json.dumps({"erreur": str(e)})
            try:
                afficher_resultat(json.loads(texte))
            except json.JSONDecodeError:
                print(f"   résultat : {texte[:400]}")
            messages.append({"role": "tool", "tool_call_id": appel.id, "content": texte})

    return "Je n'ai pas réussi à répondre en un nombre raisonnable d'étapes. Pouvez-vous reformuler ?"


async def conversation(question_unique: str | None = None) -> None:
    """Se connecte au serveur MCP puis enchaîne les questions et les réponses."""
    client, modele = config_llm()
    serveur = StdioServerParameters(command="uv", args=["run", "factures-mcp"])
    async with stdio_client(serveur) as (lecture, ecriture):
        async with ClientSession(lecture, ecriture) as session:
            await session.initialize()
            outils = [outil_mcp_vers_openai(o) for o in (await session.list_tools()).tools]
            print(f"Agent factures - modèle : {modele} ({len(outils)} outils MCP). "
                  "Tapez « quitter » pour arrêter.")
            messages = [{"role": "system", "content": CONSIGNES}]

            while True:
                if question_unique is not None:
                    question = question_unique
                else:
                    try:
                        question = input("\nVous : ").strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if question.lower() in ("quitter", "exit", "quit"):
                        break
                    if not question:
                        continue
                try:
                    reponse = await repondre(question, messages, session, outils, client, modele)
                except Exception as e:
                    reponse = f"[erreur du LLM : {type(e).__name__}: {e}]"
                    messages.pop()
                print(f"\nAgent : {reponse}")
                if question_unique is not None:
                    break
    print("\nAu revoir.")


def main() -> None:
    """Lance l'agent : conversation, ou une seule question si elle est donnée en argument."""
    question = " ".join(sys.argv[1:]) or None
    try:
        asyncio.run(conversation(question))
    except KeyboardInterrupt:
        print("\nAu revoir.")
