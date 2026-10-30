"""Moteur de workflows : graphe de nœuds exécuté en arrière-plan, état persisté en SQLite.

Types de nœuds :
- trigger   : point de départ, sa sortie est le texte saisi au lancement
- agent     : appelle un agent (lecture seule) avec un prompt modèle
- command   : propose une commande de la liste blanche et ATTEND l'approbation humaine
- condition : oriente vers la branche true/false selon la présence d'un texte
- patch     : extrait un diff du texte, le montre, puis (après approbation) l'applique et le commite sur une
              branche Git dédiée dans un worktree séparé. Le dossier de travail n'est jamais modifié.

Les modèles de texte acceptent {{input}} (sorties des nœuds amont), {{trigger}} et {{<id du nœud>}}.
"""
import json
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

from pathlib import Path

from agents import db, laravel, patching
from agents.commands import CommandRefused, store

NODE_TYPES = {"trigger", "agent", "command", "condition", "patch"}
MAX_NODES = 50
APPROVAL_TIMEOUT = 1800  # secondes d'attente d'une approbation
MAX_OUTPUT = 20000

AgentRunner = Callable[[str, str, str], tuple[str, list[str]]]  # (agent, message, repo) -> (réponse, outils)

_cancelled: set[str] = set()
_decisions: dict[tuple[str, str], str] = {}  # (run, nœud) -> approve | reject


class GraphError(Exception):
    pass


class NodeFailed(Exception):
    pass


def validate_graph(graph: dict) -> list[str]:
    """Vérifie le graphe et renvoie l'ordre d'exécution (tri topologique)."""
    nodes, edges = graph.get("nodes"), graph.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        raise GraphError("Le workflow est vide.")
    if len(nodes) > MAX_NODES:
        raise GraphError(f"Maximum {MAX_NODES} nœuds.")
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise GraphError("Identifiants de nœuds invalides ou dupliqués.")
    for n in nodes:
        if n.get("type") not in NODE_TYPES:
            raise GraphError(f"Type de nœud inconnu : {n.get('type')!r}")
    if sum(n["type"] == "trigger" for n in nodes) != 1:
        raise GraphError("Il faut exactement un déclencheur.")

    indegree = {i: 0 for i in ids}
    out = defaultdict(list)
    for e in edges:
        if e.get("source") not in indegree or e.get("target") not in indegree:
            raise GraphError("Une liaison pointe vers un nœud inexistant.")
        out[e["source"]].append(e["target"])
        indegree[e["target"]] += 1
    trigger = next(n["id"] for n in nodes if n["type"] == "trigger")
    if indegree[trigger]:
        raise GraphError("Le déclencheur ne peut pas avoir d'entrée.")

    queue = deque(i for i in ids if indegree[i] == 0)
    order = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nxt in out[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(ids):
        raise GraphError("Le workflow contient une boucle.")
    return order


def _render(template: str, ctx: dict[str, str]) -> str:
    return re.sub(r"\{\{\s*([\w.:-]+)\s*\}\}", lambda m: ctx.get(m.group(1), ""), template)


# --- Persistance ---------------------------------------------------------

def _save_run(run_id: str, status: str, states: dict, finished: bool = False) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, states = ?, finished_at = ? WHERE id = ?",
            (status, json.dumps(states, ensure_ascii=False), db.now() if finished else None, run_id),
        )


def _row_to_run(r) -> dict:
    d = dict(r)
    d["states"] = json.loads(d["states"])
    return d


def get_run(run_id: str) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(r) if r else None


def list_runs(workflow_id: str | None = None) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE (? IS NULL OR workflow_id = ?) ORDER BY created_at DESC, rowid DESC LIMIT 30",
            (workflow_id, workflow_id),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_run(r)
        out.append(d)
    return out


def recover_interrupted() -> None:
    """Au démarrage : les exécutions en cours à l'arrêt du serveur ne peuvent plus continuer."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'", (db.now(),)
        )


def cancel_run(run_id: str) -> None:
    _cancelled.add(run_id)


def has_active_run(workflow_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM runs WHERE workflow_id = ? AND status IN ('running', 'waiting') LIMIT 1", (workflow_id,)
        ).fetchone() is not None


def decide_patch(run_id: str, node_id: str, action: str) -> None:
    """Approbation humaine d'un nœud patch en attente."""
    run = get_run(run_id)
    state = run["states"].get(node_id) if run else None
    if not state or state.get("status") != "waiting_approval" or state.get("kind") != "patch":
        raise CommandRefused("Aucun patch en attente pour ce nœud.")
    _decisions[(run_id, node_id)] = action


# --- Exécution -----------------------------------------------------------

def start_run(workflow: dict, input_text: str, repo: str, agent_runner: AgentRunner) -> str:
    validate_graph(workflow["graph"])
    run_id = uuid.uuid4().hex[:10]
    states = {n["id"]: {"status": "pending"} for n in workflow["graph"]["nodes"]}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, workflow_id, status, input, repo, states, created_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, workflow["id"], "running", input_text, repo, json.dumps(states), db.now()),
        )
    threading.Thread(
        target=_execute, args=(run_id, workflow["graph"], input_text, repo, agent_runner), daemon=True
    ).start()
    return run_id


def _execute(run_id: str, graph: dict, input_text: str, repo: str, agent_runner: AgentRunner) -> None:
    order = validate_graph(graph)
    nodes = {n["id"]: n for n in graph["nodes"]}
    # Texte vide au lancement : on retombe sur la tâche enregistrée dans le déclencheur.
    trigger_node = next(n for n in graph["nodes"] if n["type"] == "trigger")
    input_text = input_text.strip() or str((trigger_node.get("data") or {}).get("input") or "").strip()
    edges = graph.get("edges", [])
    incoming = defaultdict(list)
    for e in edges:
        incoming[e["target"]].append(e)

    states: dict[str, dict] = {i: {"status": "pending"} for i in nodes}
    outputs: dict[str, str] = {}
    branches: dict[str, str | None] = {}
    final = "succeeded"

    if not input_text:
        states[trigger_node["id"]] = {
            "status": "failed",
            "error": "Aucun texte de tâche : saisis-le avant de lancer, ou renseigne « Tâche par défaut » dans le déclencheur.",
        }
        _save_run(run_id, "failed", states, finished=True)
        return

    def edge_live(e: dict) -> bool:
        src = e["source"]
        if src not in outputs:
            return False
        handle = e.get("sourceHandle")
        return not handle or handle == branches.get(src)

    try:
        for nid in order:
            if run_id in _cancelled:
                final = "cancelled"
                break
            node = nodes[nid]
            if node["type"] == "trigger":
                inputs = [input_text]
            else:
                live = [e for e in incoming[nid] if edge_live(e)]
                if not live:
                    states[nid] = {"status": "skipped"}
                    continue
                inputs = [outputs[e["source"]] for e in live]

            ctx = {"input": "\n\n".join(inputs), "trigger": input_text, **outputs}
            states[nid] = {"status": "running"}
            _save_run(run_id, "running", states)
            try:
                result = _run_node(node, ctx, repo, agent_runner, run_id, states, nid)
            except NodeFailed as exc:
                states[nid] = {**states[nid], "status": "failed", "error": str(exc)}
                final = "failed"
                break
            except Exception as exc:  # erreur imprévue d'un agent / du LLM
                states[nid] = {**states[nid], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                final = "failed"
                break
            outputs[nid] = result["output"]
            branches[nid] = result.get("branch")
            states[nid] = {**states[nid], **result, "status": "succeeded"}
            _save_run(run_id, "running", states)
    finally:
        _cancelled.discard(run_id)
        _save_run(run_id, final, states, finished=True)


def _run_node(node: dict, ctx: dict, repo: str, agent_runner: AgentRunner, run_id: str, states: dict, nid: str) -> dict:
    data = node.get("data") or {}
    kind = node["type"]

    if kind == "trigger":
        return {"output": ctx["trigger"]}

    if kind == "agent":
        prompt = _render(data.get("prompt") or "{{input}}", ctx)
        if not prompt.strip():
            raise NodeFailed("Prompt vide.")
        def note(msg: str) -> None:
            states[nid] = {**states[nid], "waiting_note": msg} if msg else {k: v for k, v in states[nid].items() if k != "waiting_note"}
            _save_run(run_id, "running", states)

        laravel.cancel_check.set(lambda: run_id in _cancelled)
        laravel.progress_hook.set(note)
        try:
            answer, tools_used = agent_runner(data.get("agent") or "team", prompt, repo)
        except laravel.Cancelled:
            raise NodeFailed("Annulé.")
        return {"output": answer[:MAX_OUTPUT], "tools": tools_used}

    if kind == "condition":
        needle = _render(data.get("contains") or "", ctx).lower()
        hit = bool(needle) and needle in ctx["input"].lower()
        return {"output": ctx["input"], "branch": "true" if hit else "false"}

    if kind == "command":
        command = _render(data.get("command") or "", ctx).strip()
        cwd = _repo_path(repo)
        if data.get("patch_node"):
            worktree = (states.get(data["patch_node"]) or {}).get("worktree")
            if not worktree:
                raise NodeFailed("Le nœud patch choisi n'a pas produit de branche (est-il en amont et approuvé ?).")
            cwd = Path(worktree).resolve()
            if patching.WORKTREES.resolve() not in cwd.parents:
                raise NodeFailed("Worktree hors de la zone autorisée.")
        try:
            proposal = store.propose(cwd, command, data.get("reason") or f"Workflow {run_id}")
        except CommandRefused as exc:
            raise NodeFailed(f"Commande refusée par la liste blanche : {exc}")
        states[nid] = {"status": "waiting_approval", "kind": "command", "proposal_id": proposal.id, "command": command}
        _save_run(run_id, "waiting", states)
        deadline = time.monotonic() + APPROVAL_TIMEOUT
        while True:
            current = store.get(proposal.id)
            if current.status != "pending":
                break
            if run_id in _cancelled or time.monotonic() > deadline:
                try:
                    store.reject(proposal.id)
                except CommandRefused:
                    pass
                raise NodeFailed("Annulé." if run_id in _cancelled else "Délai d'approbation dépassé.")
            time.sleep(1)
        if current.status == "rejected":
            raise NodeFailed("Commande refusée par l'utilisateur.")
        _save_run(run_id, "running", states)
        return {
            "output": (current.output or "")[:MAX_OUTPUT],
            "branch": "success" if current.exit_code == 0 else "failure",
            "proposal_id": proposal.id,
            "command": command,
            "exit_code": current.exit_code,
        }

    if kind == "patch":
        text = _render(data.get("source") or "{{input}}", ctx)
        root = _repo_path(repo)
        try:
            diff = patching.extract_diff(text)
            files = patching.preflight(root, diff)
        except patching.PatchError as exc:
            raise NodeFailed(str(exc))
        states[nid] = {"status": "waiting_approval", "kind": "patch", "diff": diff, "files": files}
        _save_run(run_id, "waiting", states)
        _decisions.pop((run_id, nid), None)
        deadline = time.monotonic() + APPROVAL_TIMEOUT
        while True:
            decision = _decisions.pop((run_id, nid), None)
            if decision:
                break
            if run_id in _cancelled or time.monotonic() > deadline:
                raise NodeFailed("Annulé." if run_id in _cancelled else "Délai d'approbation dépassé.")
            time.sleep(1)
        if decision == "reject":
            raise NodeFailed("Correctif refusé par l'utilisateur.")
        try:
            applied = patching.apply_to_branch(root, diff, run_id, nid, data.get("message") or f"Correctif agent ({run_id})")
        except patching.PatchError as exc:
            raise NodeFailed(str(exc))
        _save_run(run_id, "running", states)
        return {
            "output": (f"Correctif appliqué sur la branche {applied['git_branch']} (worktree {applied['worktree']}).\n"
                       f"Fichiers : {', '.join(applied['files'])}\n{applied['stat']}"),
            "diff": diff, **applied,
        }

    raise NodeFailed(f"Type de nœud non géré : {kind}")


def _repo_path(repo: str):
    from pathlib import Path

    return Path(repo).expanduser().resolve()


# --- CRUD ----------------------------------------------------------------

def save_workflow(name: str, repo: str | None, graph: dict, workflow_id: str | None = None) -> str:
    validate_graph(graph)
    with db.connect() as conn:
        if workflow_id:
            done = conn.execute(
                "UPDATE workflows SET name = ?, repo = ?, graph = ?, updated_at = ? WHERE id = ?",
                (name, repo, json.dumps(graph, ensure_ascii=False), db.now(), workflow_id),
            ).rowcount
            if not done:
                raise KeyError(workflow_id)
            return workflow_id
        new_id = uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO workflows (id, name, repo, graph, updated_at) VALUES (?,?,?,?,?)",
            (new_id, name, repo, json.dumps(graph, ensure_ascii=False), db.now()),
        )
    return new_id


def get_workflow(workflow_id: str) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["graph"] = json.loads(d["graph"])
    return d


def list_workflows() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT id, name, repo, updated_at FROM workflows ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_workflow(workflow_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,)).rowcount > 0
