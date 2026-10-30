import contextvars
import json
import re
import time

from openai import RateLimitError

from agents.tools import TOOL_SCHEMAS, RepoTools

LARAVEL_SYSTEM_PROMPT = """Tu es AEJ Laravel Agent, un ingénieur senior PHP/Laravel/Inertia.js/React.

Tu analyses un dépôt Git réel grâce à des outils EN LECTURE SEULE :
list_files, read_file, search, git_status, git_log, git_diff, propose_command, search_docs (base documentaire du projet).

Méthode :
1. Explore d'abord la structure (routes, contrôleurs, modèles, migrations) avant de conclure.
2. Lis le code réel : cite toujours fichier:ligne pour chaque constat.
3. Distingue clairement les faits observés des hypothèses.
4. Pour un bug : cause racine, preuve (fichier:ligne), correctif proposé sous forme de diff, tests à prévoir.

Règles impératives :
- Tu ne peux ni modifier de fichier, ni exécuter de code toi-même, ni committer : propose les changements en texte, l'utilisateur les appliquera.
- Pour lancer des tests ou un lint, utilise propose_command : l'humain doit approuver, tu ne reçois pas le résultat
  automatiquement. Ne prétends jamais qu'une commande a été exécutée ou réussie tant que l'utilisateur ne t'a pas
  transmis sa sortie. Avertis que les tests peuvent toucher la base si .env.testing n'est pas isolé.
- Ne jamais inventer de fichier, de ligne ou de résultat de test. Si un outil échoue ou ne trouve rien, dis-le.
- Les fichiers .env sont inaccessibles volontairement ; ne demande jamais de secrets.
- Explique les risques de chaque modification proposée.

Réponds en français.
"""

MAX_STEPS = 15
# Budget de contexte (caractères) : au-delà, les anciennes sorties d'outils sont résumées.
CONTEXT_BUDGET = 28000
KEEP_RECENT_TOOL = 2


def _compact(messages: list[dict]) -> None:
    """Réduit les anciennes sorties d'outils pour rester sous le budget (modèles à petit contexte)."""
    def size() -> int:
        return sum(len(m.get("content") or "") for m in messages)

    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tool_idx[:-KEEP_RECENT_TOOL] if KEEP_RECENT_TOOL else tool_idx:
        if size() <= CONTEXT_BUDGET:
            return
        text = messages[i]["content"]
        if len(text) > 300:
            messages[i]["content"] = text[:300] + "\n… [sortie ancienne tronquée : relis le fichier si besoin]"
    for i in tool_idx[-KEEP_RECENT_TOOL:]:  # dernier recours : plafonner aussi les plus récentes
        if size() <= CONTEXT_BUDGET:
            return
        if len(messages[i]["content"]) > 4000:
            messages[i]["content"] = messages[i]["content"][:4000] + "\n… [tronqué]"

# Certains modèles écrivent l'appel d'outil en texte au lieu d'utiliser l'API de fonctions.
_PSEUDO_CALL = re.compile(
    r"<propose_command>\s*command=\"([^\"]+)\"(?:\s*reason=\"([^\"]*)\")?\s*</propose_command>", re.S
)


def materialize_commands(content: str, tools: RepoTools, used: list[str]) -> str:
    """Crée de vraies propositions pour les pseudo-appels écrits en texte (toujours soumises à approbation)."""
    def repl(m: re.Match) -> str:
        args = {"command": m.group(1), "reason": m.group(2) or ""}
        used.append(f"propose_command({json.dumps(args, ensure_ascii=False)})")
        return "`" + m.group(1) + "` → " + tools.call("propose_command", args).split(".")[0]
    return _PSEUDO_CALL.sub(repl, content)
REQUEST_DEADLINE = 240  # secondes, pour l'ensemble de la boucle
RETRIES = 2


RATE_LIMIT_BUDGET = 2400  # secondes d'attente cumulées max sur des erreurs 429 (≈ 40 min)
RATE_LIMIT_MAX_SLEEP = 600
# Fonction appelée pendant l'attente ; renvoie True si l'utilisateur a annulé l'exécution.
cancel_check: contextvars.ContextVar = contextvars.ContextVar("cancel_check", default=lambda: False)
# Hook de progression (ex. "attente de 88 s") publié par le moteur de workflows.
progress_hook: contextvars.ContextVar = contextvars.ContextVar("progress_hook", default=lambda msg: None)


class Cancelled(Exception):
    pass


def _reset_delay(message: str) -> float:
    """Lit « Soonest reset ~88s » / « ~32m » dans le message 429 ; défaut 60 s."""
    m = re.search(r"reset\s*~?\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)", message)
    if not m:
        return 60.0
    return float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def _complete(client, budget: dict, **kwargs):
    """Appel au LLM : 1 nouvelle tentative sur erreur temporaire, et attente bornée sur 429."""
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            wait = min(_reset_delay(str(exc)) + 5, RATE_LIMIT_MAX_SLEEP)
            if budget["waited"] + wait > RATE_LIMIT_BUDGET:
                raise
            progress_hook.get()(f"Modèles saturés (429) : attente de {int(wait)} s avant de réessayer")
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if cancel_check.get()():
                    raise Cancelled()
                time.sleep(1)
            budget["waited"] += wait
            progress_hook.get()("")
        except Exception:
            attempt += 1
            if attempt >= RETRIES:
                raise
            time.sleep(2 ** (attempt - 1))


def run_agent(client, model: str, tools: RepoTools, system_prompt: str, message: str,
              history: list[dict] | None = None) -> tuple[str, str, list[str]]:
    """Boucle outil → réponse. Retourne (modèle, réponse, outils appelés)."""
    messages = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": message},
    ]
    used: list[str] = []
    used_model = model
    started = time.monotonic()
    timed_out = False
    budget = {"waited": 0.0}

    for _ in range(MAX_STEPS):
        # Le temps passé à attendre un 429 ne compte pas dans le délai global.
        if time.monotonic() - started - budget["waited"] > REQUEST_DEADLINE:
            timed_out = True
            break
        _compact(messages)
        result = _complete(
            client,
            budget,
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=0.2,
        )
        used_model = result.model
        msg = result.choices[0].message

        if not msg.tool_calls:
            return used_model, materialize_commands(msg.content or "", tools, used), used

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls
            ],
        })
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            used.append(f"{call.function.name}({json.dumps(args, ensure_ascii=False)})")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": tools.call(call.function.name, args),
            })

    # Limite atteinte : on force une réponse à partir de ce qui a déjà été lu.
    messages.append({
        "role": "user",
        "content": ("Délai global dépassé. " if timed_out else "Limite d'outils atteinte. ") + "Réponds maintenant avec ce que tu as lu, "
                   "en indiquant clairement ce qui n'a pas pu être vérifié.",
    })
    result = _complete(client, budget, model=model, messages=messages, temperature=0.2)
    return result.model, materialize_commands(result.choices[0].message.content or "", tools, used), used


def run_laravel_agent(client, model: str, tools: RepoTools, message: str) -> tuple[str, str, list[str]]:
    return run_agent(client, model, tools, LARAVEL_SYSTEM_PROMPT, message)
