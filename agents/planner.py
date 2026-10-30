"""Propose des workflows à partir d'une tâche décrite en langage naturel et d'un projet connecté.

Déterministe (pas d'appel LLM) : la tâche est classée par mots-clés, la pile du projet choisit
l'agent et la commande de vérification. L'utilisateur choisit puis confirme ; rien ne s'exécute ici.
"""
import re
import unicodedata

NODE_W, NODE_H_GAP = 270, 190

_KEYWORDS = {
    "bugfix": ("bug", "erreur", "error", "exception", "corrige", "corriger", "fix", "plante", "crash", "echoue", "ne marche", "sqlstate", "500", "regression"),
    "feature": ("ajoute", "ajouter", "nouvelle", "nouveau", "fonctionnalite", "feature", "implemente", "implementer", "cree", "creer", "developpe", "page", "endpoint"),
    "review": ("revue", "review", "relis", "qualite", "refactor", "lisibilite", "dette", "propre", "analyse le code"),
    "audit": ("securite", "audit", "vulnerab", "faille", "injection", "xss", "csrf", "permission", "fuite", "owasp"),
    "tests": ("test", "tests", "couverture", "phpunit", "verifier", "verification", "ci "),
}


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def pick_agent(project: dict, task: str) -> str:
    stacks, t = set(project["stacks"]), _norm(task)
    front_words = ("react", "tsx", "jsx", "composant", "component", "inertia", "tailwind", "interface", "ecran css")
    if {"laravel", "php"} & stacks:
        return "react" if ({"react", "inertia"} & stacks and any(w in t for w in front_words)) else "laravel"
    if "flutter" in stacks:
        return "flutter"
    if {"react", "next", "typescript", "node"} & stacks:
        return "react"
    if "docker" in stacks:
        return "devops"
    return "team"


class _Builder:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._n = 0

    def add(self, kind: str, col: int, row: int, **data) -> str:
        self._n += 1
        nid = f"n{self._n}"
        self.nodes.append({"id": nid, "type": kind, "position": {"x": col * NODE_W, "y": 60 + row * NODE_H_GAP}, "data": data})
        return nid

    def link(self, a: str, b: str, handle: str | None = None) -> None:
        self.edges.append({"source": a, "target": b, "sourceHandle": handle})

    def graph(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges}


_DIFF_RULES = (
    "Réponds UNIQUEMENT avec un diff au format `git diff` (lignes `--- a/chemin`, `+++ b/chemin`, hunks `@@`) "
    "dans un bloc ```diff, avec 3 lignes de contexte copiées exactement du fichier lu. "
    "Ne touche ni .env, ni vendor, ni node_modules."
)


def _bugfix(agent: str, check: str | None) -> dict:
    b = _Builder()
    t = b.add("trigger", 0, 1, label="Tâche")
    a = b.add("agent", 1, 1, label="Diagnostic + correctif", agent=agent,
              prompt="Trouve la cause racine du problème ci-dessous en lisant le code (cite fichier:ligne), puis propose le correctif minimal.\n"
                     + _DIFF_RULES + "\n\nProblème :\n{{input}}")
    p = b.add("patch", 2, 1, label="Appliquer sur une branche", source="{{input}}", message="Correctif proposé par un agent")
    b.link(t, a); b.link(a, p)
    if check:
        c = b.add("command", 3, 1, label="Vérifications dans la branche", command=check, reason="Vérifier le correctif", patch_node=p)
        ok = b.add("agent", 4, 0, label="Bilan (OK)", agent=agent, prompt="Les vérifications passent. Résume le correctif et ses risques :\n{{" + p + "}}\n\nSortie :\n{{" + c + "}}")
        ko = b.add("agent", 4, 2, label="Analyse de l'échec", agent=agent, prompt="Les vérifications échouent après ce correctif :\n{{" + p + "}}\n\nSortie :\n{{" + c + "}}\n\nExplique pourquoi (fichier:ligne) et propose un nouveau diff.")
        b.link(p, c); b.link(c, ok, "success"); b.link(c, ko, "failure")
    return b.graph()


def _feature(agent: str, check: str | None) -> dict:
    b = _Builder()
    t = b.add("trigger", 0, 1, label="Tâche")
    plan = b.add("agent", 1, 1, label="Plan (lecture seule)", agent=agent,
                 prompt="Étudie l'architecture existante du projet (routes, contrôleurs, modèles, composants, conventions) puis propose un plan d'implémentation court : fichiers à créer/modifier, risques, tests à prévoir.\n\nDemande :\n{{input}}")
    code = b.add("agent", 2, 1, label="Rédige le code", agent=agent,
                 prompt="Implémente le plan ci-dessous en respectant les conventions du dépôt.\n" + _DIFF_RULES + "\n\nDemande :\n{{trigger}}\n\nPlan :\n{{" + plan + "}}")
    p = b.add("patch", 3, 1, label="Appliquer sur une branche", source="{{input}}", message="Fonctionnalité proposée par un agent")
    b.link(t, plan); b.link(plan, code); b.link(code, p)
    if check:
        c = b.add("command", 4, 1, label="Vérifications dans la branche", command=check, reason="Vérifier la fonctionnalité", patch_node=p)
        ko = b.add("agent", 5, 1, label="Analyse de l'échec", agent=agent, prompt="Les vérifications échouent :\n{{" + c + "}}\n\nCode appliqué :\n{{" + p + "}}\n\nExplique pourquoi et propose un diff correctif.")
        b.link(p, c); b.link(c, ko, "failure")
    return b.graph()


def _readonly(agent: str, label: str, prompt: str) -> dict:
    b = _Builder()
    t = b.add("trigger", 0, 0, label="Tâche")
    a = b.add("agent", 1, 0, label=label, agent=agent, prompt=prompt)
    b.link(t, a)
    return b.graph()


def _review(agent: str, check: str | None) -> dict:
    return _readonly(agent, "Revue de code", "Fais une revue de code de ce qui est demandé : lis les fichiers concernés, relève bugs probables, dette technique et problèmes de lisibilité. Cite fichier:ligne, classe par gravité, et propose des corrections sous forme de diff (sans les appliquer).\n\nPérimètre :\n{{input}}")


def _audit(agent: str, check: str | None) -> dict:
    return _readonly(agent, "Audit sécurité", "Fais un audit de sécurité du périmètre ci-dessous (injections SQL, XSS, CSRF, autorisations, validation, fuite de données, secrets). Lis le code réel, cite fichier:ligne, indique la gravité et le correctif conseillé. Distingue les faits des hypothèses.\n\nPérimètre :\n{{input}}")


def _tests(agent: str, check: str | None) -> dict:
    b = _Builder()
    t = b.add("trigger", 0, 1, label="Tâche")
    c = b.add("command", 1, 1, label="Vérifications", command=check, reason="Contrôle demandé")
    ok = b.add("agent", 2, 0, label="Bilan (OK)", agent=agent, prompt="Les vérifications passent. Résume l'état :\n{{" + c + "}}")
    ko = b.add("agent", 2, 2, label="Analyse des échecs", agent=agent, prompt="Les vérifications échouent :\n{{" + c + "}}\n\nPour chaque échec : cause racine probable (fichier:ligne) et correctif proposé en diff.")
    b.link(t, c); b.link(c, ok, "success"); b.link(c, ko, "failure")
    return b.graph()


TEMPLATES = {
    "bugfix": ("Corriger un bug", "Diagnostic → diff → branche dédiée → vérifications → bilan", _bugfix, False),
    "feature": ("Implémenter une fonctionnalité", "Plan → code → branche dédiée → vérifications", _feature, False),
    "review": ("Revue de code", "Lecture seule : bugs, dette, lisibilité", _review, False),
    "audit": ("Audit de sécurité", "Lecture seule : injections, accès, secrets", _audit, False),
    "tests": ("Lancer les vérifications", "Commande approuvée → analyse des échecs", _tests, True),
}


def _score(task: str) -> dict[str, int]:
    t = _norm(task)
    return {k: sum(1 for w in words if re.search(r"\b" + re.escape(w), t) or w in t) for k, words in _KEYWORDS.items()}


def propose(project: dict, task: str) -> list[dict]:
    """Retourne les workflows possibles, le plus pertinent en premier."""
    agent = pick_agent(project, task)
    check = project.get("check_command")
    scores = _score(task)
    out = []
    for key, (title, summary, builder, needs_check) in TEMPLATES.items():
        if needs_check and not check:
            continue
        graph = builder(agent, check)
        steps = [n["data"].get("label", n["type"]) for n in graph["nodes"]]
        writes = any(n["type"] == "patch" for n in graph["nodes"])
        out.append({
            "template": key,
            "title": title,
            "summary": summary,
            "agent": agent,
            "steps": steps,
            "writes_code": writes,
            "approvals": sum(n["type"] in ("patch", "command") for n in graph["nodes"]),
            "score": scores.get(key, 0),
            "graph": graph,
        })
    # Pertinence : mots-clés d'abord ; à égalité, bugfix puis revue (ordre du dictionnaire).
    out.sort(key=lambda p: -p["score"])
    for i, p in enumerate(out):
        p["recommended"] = i == 0 and p["score"] > 0
    return out
