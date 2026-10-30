import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field

from agents.developer import DEVELOPER_SYSTEM_PROMPT
from agents import db, planner, projects, triggers, workflows
from agents.commands import CommandRefused, store
from agents.team import route, run_specialist
from agents.tools import RepoTools, ToolError

load_dotenv()

client = OpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:3001/v1"),
    api_key=os.getenv("LLM_API_KEY", ""),
    timeout=60.0,
    max_retries=0,  # les nouvelles tentatives sont gérées (et bornées) dans agents/laravel.py
)
MODEL = os.getenv("LLM_MODEL", "auto")

app = FastAPI(
    title="Local AI Agents",
    description="Plateforme d'agents IA développeurs via FreeLLMAPI",
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


class ChatResponse(BaseModel):
    agent: str
    model: str
    answer: str


@app.get("/health")
def health():
    return {"status": "ok", "service": "Local AI Agents"}


@app.post("/agents/developer/chat", response_model=ChatResponse)
def developer_chat(request: ChatRequest):
    try:
        result = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": DEVELOPER_SYSTEM_PROMPT},
                {"role": "user", "content": request.message},
            ],
            temperature=0.2,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erreur FreeLLMAPI : {exc}")

    return ChatResponse(
        agent="AEJ Developer Agent",
        model=result.model,
        answer=result.choices[0].message.content or "",
    )


class RepoChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    repo: str | None = Field(default=None, description="Chemin du dépôt ; défaut : REPO_ROOT")
    conversation_id: str | None = Field(default=None, description="Reprend une conversation existante")


class RepoChatResponse(ChatResponse):
    tools_used: list[str]
    conversation_id: str


def _open_repo(request: RepoChatRequest) -> RepoTools:
    repo = request.repo or os.getenv("REPO_ROOT")
    if not repo:
        raise HTTPException(status_code=400, detail="Aucun dépôt : renseigne REPO_ROOT ou 'repo'.")
    try:
        return RepoTools(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _run_team_agent(name: str, request: RepoChatRequest) -> RepoChatResponse:
    tools = _open_repo(request)
    cid = request.conversation_id
    if cid and not db.conversation_exists(cid):
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    past = db.history(cid) if cid else []
    try:
        label, model, answer, used = run_specialist(client, MODEL, tools, name, request.message, past)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erreur FreeLLMAPI : {exc}")
    cid = cid or db.new_conversation(label, str(tools.root))
    db.add_messages(cid, [("user", request.message), ("assistant", answer)])
    return RepoChatResponse(agent=label, model=model, answer=answer, tools_used=used, conversation_id=cid)


@app.post("/agents/laravel/chat", response_model=RepoChatResponse)
def laravel_chat(request: RepoChatRequest):
    return _run_team_agent("laravel", request)


@app.post("/agents/react/chat", response_model=RepoChatResponse)
def react_chat(request: RepoChatRequest):
    return _run_team_agent("react", request)


@app.post("/agents/flutter/chat", response_model=RepoChatResponse)
def flutter_chat(request: RepoChatRequest):
    return _run_team_agent("flutter", request)


@app.post("/agents/devops/chat", response_model=RepoChatResponse)
def devops_chat(request: RepoChatRequest):
    return _run_team_agent("devops", request)


@app.post("/team/chat", response_model=RepoChatResponse)
def team_chat(request: RepoChatRequest):
    """Orchestrateur : choisit l'agent adapté puis lui délègue la demande."""
    try:
        name = route(client, MODEL, request.message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erreur FreeLLMAPI : {exc}")
    return _run_team_agent(name, request)


@app.get("/commands")
def list_commands(status: str | None = None):
    return store.list(status)


@app.post("/commands/{proposal_id}/approve")
def approve_command(proposal_id: str):
    """Exécute la commande proposée — seule porte d'entrée de toute exécution."""
    try:
        return store.approve(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposition introuvable.")
    except CommandRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/commands/{proposal_id}/reject")
def reject_command(proposal_id: str):
    try:
        return store.reject(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposition introuvable.")
    except CommandRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/conversations")
def conversations():
    return db.list_conversations()


@app.get("/conversations/{conversation_id}")
def conversation(conversation_id: str):
    if not db.conversation_exists(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return db.get_conversation(conversation_id)


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    if not db.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return {"deleted": conversation_id}


class DocRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200000)


class DocImportRequest(BaseModel):
    path: str = Field(description="Fichier texte/markdown relatif au dépôt")
    repo: str | None = None
    title: str | None = None


@app.post("/knowledge")
def add_doc(request: DocRequest):
    return {"fragments": db.add_doc(request.title, request.content)}


@app.post("/knowledge/import")
def import_doc(request: DocImportRequest):
    """Indexe un fichier du dépôt (mêmes protections que les agents : pas de .env, pas hors dépôt)."""
    tools = _open_repo(RepoChatRequest(message="-", repo=request.repo))
    if Path(request.path).suffix.lower() not in {".md", ".txt", ".rst"}:
        raise HTTPException(status_code=400, detail="Seuls .md, .txt et .rst sont importables.")
    try:
        text = tools.read_text(request.path)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"fragments": db.add_doc(request.title or Path(request.path).name, text, source=request.path)}


@app.get("/knowledge")
def docs():
    return db.list_docs()


@app.get("/knowledge/search")
def search_docs(q: str, limit: int = 5):
    return db.search_docs(q, min(max(limit, 1), 20))


@app.delete("/knowledge/{doc_id}")
def delete_doc(doc_id: int):
    if not db.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="Document introuvable.")
    return {"deleted": doc_id}


# --- Workflows -------------------------------------------------------------

def _launch(workflow_id: str, input_text: str, repo_override: str | None = None) -> str:
    """Point d'entrée unique pour lancer un workflow (manuel, planifié, webhook)."""
    wf = workflows.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow introuvable.")
    repo = repo_override or wf["repo"] or os.getenv("REPO_ROOT")
    if not repo:
        raise HTTPException(status_code=400, detail="Aucun dépôt : renseigne le dépôt du workflow ou REPO_ROOT.")
    if workflows.has_active_run(workflow_id):
        raise HTTPException(status_code=409, detail="Une exécution de ce workflow est déjà en cours.")
    try:
        repo = str(RepoTools(repo).root)
        return workflows.start_run(wf, input_text, repo, _workflow_agent)
    except (ValueError, workflows.GraphError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.on_event("startup")
def _startup():
    workflows.recover_interrupted()
    triggers.start_scheduler(lambda wid, text: _launch(wid, text))


def _workflow_agent(agent: str, message: str, repo: str) -> tuple[str, list[str]]:
    """Exécute un agent pour un nœud de workflow (lecture seule, même garde-fous que le chat)."""
    tools = RepoTools(repo)
    name = route(client, MODEL, message) if agent == "team" else agent
    if name not in ("laravel", "react", "flutter", "devops"):
        raise ValueError(f"Agent inconnu : {agent}")
    _, _, answer, used = run_specialist(client, MODEL, tools, name, message)
    return answer, used


class WorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repo: str | None = None
    graph: dict


class RunRequest(BaseModel):
    input: str = Field(default="", max_length=12000)
    repo: str | None = None


@app.get("/workflows")
def list_workflows():
    return workflows.list_workflows()


@app.post("/workflows")
def create_workflow(request: WorkflowRequest):
    try:
        return {"id": workflows.save_workflow(request.name, request.repo, request.graph)}
    except workflows.GraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    wf = workflows.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow introuvable.")
    return wf


@app.put("/workflows/{workflow_id}")
def update_workflow(workflow_id: str, request: WorkflowRequest):
    try:
        return {"id": workflows.save_workflow(request.name, request.repo, request.graph, workflow_id)}
    except workflows.GraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Workflow introuvable.")


@app.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str):
    if not workflows.delete_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="Workflow introuvable.")
    return {"deleted": workflow_id}


@app.post("/workflows/{workflow_id}/run")
def run_workflow(workflow_id: str, request: RunRequest):
    return {"run_id": _launch(workflow_id, request.input, request.repo)}


class TriggerRequest(BaseModel):
    kind: str
    every_minutes: int | None = None
    daily_at: str | None = None
    enabled: bool = False
    input: str = Field(default="", max_length=12000)


def _public_trigger(t: dict) -> dict:
    return {**t, "webhook_path": f"/hooks/{t['webhook_token']}" if t["webhook_token"] else None}


@app.get("/workflows/{workflow_id}/trigger")
def get_trigger(workflow_id: str):
    if workflows.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow introuvable.")
    return _public_trigger(triggers.get(workflow_id))


@app.put("/workflows/{workflow_id}/trigger")
def put_trigger(workflow_id: str, request: TriggerRequest):
    if workflows.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow introuvable.")
    try:
        saved = triggers.save(workflow_id, request.kind, request.every_minutes, request.daily_at,
                              request.enabled, request.input)
    except triggers.TriggerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _public_trigger(saved)


@app.post("/workflows/{workflow_id}/trigger/rotate")
def rotate_trigger(workflow_id: str):
    try:
        return _public_trigger(triggers.rotate_token(workflow_id))
    except triggers.TriggerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/hooks/{token}", status_code=202)
async def webhook(token: str, request: Request):
    """Lance le workflow lié au jeton. Le corps de la requête devient {{trigger}} (12 000 caractères max)."""
    workflow_id = triggers.workflow_for_token(token)
    if workflow_id is None:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.body()
    if len(body) > 12000:
        raise HTTPException(status_code=413, detail="Corps trop volumineux (12 000 octets max).")
    text = body.decode("utf-8", errors="replace").strip() or triggers.get(workflow_id)["input"]
    return {"run_id": _launch(workflow_id, text)}


@app.get("/runs")
def list_runs(workflow_id: str | None = None):
    return workflows.list_runs(workflow_id)


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = workflows.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Exécution introuvable.")
    return run


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    if workflows.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Exécution introuvable.")
    workflows.cancel_run(run_id)
    return {"cancelling": run_id}


@app.post("/runs/{run_id}/nodes/{node_id}/{action}")
def decide_node(run_id: str, node_id: str, action: str):
    """Approuve ou refuse l'application d'un correctif (nœud patch en attente)."""
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=404, detail="Action inconnue.")
    try:
        workflows.decide_patch(run_id, node_id, action)
    except CommandRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"decision": action}


# --- Projets & propositions de workflows ----------------------------------

class ScanRequest(BaseModel):
    roots: list[str] | None = Field(default=None, max_length=10)


class ConnectRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


@app.get("/projects")
def list_projects():
    return projects.connected()


@app.post("/projects/scan")
def scan_projects(request: ScanRequest):
    """Détecte les dépôts Git (lecture seule) ; indique lesquels sont déjà connectés."""
    roots = [Path(r) for r in request.roots] if request.roots else None
    known = {p["path"] for p in projects.connected()}
    found = projects.scan(roots)
    return {
        "roots": [str(r) for r in (roots or projects.default_roots())],
        "projects": [{**p, "connected": p["path"] in known} for p in found],
    }


@app.post("/projects")
def connect_project(request: ConnectRequest):
    try:
        return projects.connect(request.path)
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/projects/{project_id}")
def disconnect_project(project_id: str):
    if not projects.disconnect(project_id):
        raise HTTPException(status_code=404, detail="Projet introuvable.")
    return {"deleted": project_id}


class ProposeRequest(BaseModel):
    project_id: str
    task: str = Field(min_length=3, max_length=4000)


class CreateFromProposalRequest(ProposeRequest):
    template: str
    launch: bool = False


def _project_or_404(project_id: str) -> dict:
    project = projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Projet introuvable.")
    return project


@app.post("/planner/propose")
def propose_workflows(request: ProposeRequest):
    project = _project_or_404(request.project_id)
    return {"project": project, "proposals": planner.propose(project, request.task)}


@app.post("/planner/create")
def create_from_proposal(request: CreateFromProposalRequest):
    project = _project_or_404(request.project_id)
    chosen = next((p for p in planner.propose(project, request.task) if p["template"] == request.template), None)
    if chosen is None:
        raise HTTPException(status_code=400, detail="Modèle inconnu ou indisponible pour ce projet.")
    for node in chosen["graph"]["nodes"]:
        if node["type"] == "trigger":
            node["data"]["input"] = request.task  # la tâche reste attachée au workflow
    short = " ".join(request.task.split())[:50]
    try:
        wid = workflows.save_workflow(f"{project['name']} · {chosen['title']} — {short}", project["path"], chosen["graph"])
    except workflows.GraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result: dict = {"workflow_id": wid, "run_id": None}
    if request.launch:
        result["run_id"] = _launch(wid, request.task)
    return result
