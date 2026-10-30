"""Agents spécialisés (React, Flutter, DevOps) et orchestrateur."""
from agents.laravel import LARAVEL_SYSTEM_PROMPT, run_agent
from agents.tools import RepoTools

_COMMON_RULES = """
Tu analyses un dépôt Git réel grâce à des outils EN LECTURE SEULE :
list_files, read_file, search, git_status, git_log, git_diff, propose_command, search_docs (base documentaire)
(propose_command ne fait que proposer : l'humain approuve, tu ne vois pas la sortie sauf s'il te la transmet).

Règles impératives :
- Explore avant de conclure ; cite fichier:ligne pour chaque constat.
- Distingue faits observés et hypothèses.
- Tu ne modifies rien et n'exécutes rien : propose les changements en diff texte.
- Ne jamais inventer de fichier, de ligne ou de résultat de test.
- Les fichiers .env sont inaccessibles volontairement ; ne demande jamais de secrets.
- Explique les risques de chaque modification proposée.
Réponds en français.
"""

REACT_SYSTEM_PROMPT = (
    "Tu es AEJ React Agent, un ingénieur senior React, TypeScript, Inertia.js et Tailwind. "
    "Tu vérifies typage, gestion d'état, props Inertia, accessibilité et performance des composants." + _COMMON_RULES
)
FLUTTER_SYSTEM_PROMPT = (
    "Tu es AEJ Flutter Agent, un ingénieur senior Flutter/Dart. "
    "Tu vérifies architecture, gestion d'état, appels API, null-safety et performance des widgets." + _COMMON_RULES
)
DEVOPS_SYSTEM_PROMPT = (
    "Tu es AEJ DevOps Agent, un ingénieur senior Linux, Docker, Nginx et CI/CD. "
    "Tu vérifies Dockerfiles, configurations de déploiement, sécurité et fiabilité." + _COMMON_RULES
)

AGENTS = {
    "laravel": ("AEJ Laravel Agent", LARAVEL_SYSTEM_PROMPT),
    "react": ("AEJ React Agent", REACT_SYSTEM_PROMPT),
    "flutter": ("AEJ Flutter Agent", FLUTTER_SYSTEM_PROMPT),
    "devops": ("AEJ DevOps Agent", DEVOPS_SYSTEM_PROMPT),
}

ROUTER_PROMPT = """Tu es un routeur. Classe la demande de développement dans UNE catégorie :
- laravel : PHP, Laravel, contrôleurs, modèles, migrations, routes, SQL, API backend
- react : React, TypeScript, Inertia.js (pages/composants), CSS, Tailwind
- flutter : Flutter, Dart, application mobile
- devops : Docker, Nginx, Linux, déploiement, CI/CD
Réponds par un seul mot : laravel, react, flutter ou devops."""

_KEYWORDS = {
    "flutter": ("flutter", "dart", "widget", "pubspec"),
    "devops": ("docker", "nginx", "deploy", "déploiement", "ci/cd", "linux", "supervisor"),
    "react": ("react", "tsx", "jsx", "typescript", "inertia", "tailwind", "composant"),
}


def route(client, model: str, message: str) -> str:
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": message}],
            temperature=0,
            max_tokens=200,
        )
        answer = (r.choices[0].message.content or "").lower()
        for name in AGENTS:
            if name in answer:
                return name
    except Exception:
        pass
    lowered = message.lower()
    for name, words in _KEYWORDS.items():
        if any(w in lowered for w in words):
            return name
    return "laravel"


def run_specialist(client, model: str, tools: RepoTools, name: str, message: str, history=None):
    label, prompt = AGENTS[name]
    used_model, answer, used = run_agent(client, model, tools, prompt, message, history)
    return label, used_model, answer, used
