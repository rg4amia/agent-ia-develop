import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api, Graph, NodeKind, NodeState, Run, TriggerConfig, Workflow, WorkflowSummary, wfApi } from "./api";

type NodeData = {
  label: string;
  agent?: string;
  prompt?: string;
  command?: string;
  reason?: string;
  contains?: string;
  source?: string;
  input?: string;
  message?: string;
  patch_node?: string;
  status?: string;
  waitingNote?: string;
};
type StepNode = Node<NodeData>;

const KINDS: Record<NodeKind, { title: string; color: string; hint: string }> = {
  trigger: { title: "Déclencheur", color: "#7c5cff", hint: "Point de départ : reçoit le texte saisi au lancement." },
  agent: { title: "Agent", color: "#2457d6", hint: "Interroge un agent (lecture seule) avec un prompt." },
  command: { title: "Commande", color: "#d97706", hint: "Propose une commande ; le flux attend ton approbation." },
  condition: { title: "Condition", color: "#0e9f6e", hint: "Oriente vers « oui » ou « non » selon un texte." },
  patch: { title: "Appliquer le correctif", color: "#dc2626", hint: "Extrait un diff, te le montre, puis l'applique sur une branche Git dédiée (ton dossier n'est pas touché)." },
};

const STATUS_LABEL: Record<string, string> = {
  running: "en cours",
  waiting_approval: "approbation requise",
  succeeded: "terminé",
  failed: "échec",
  skipped: "ignoré",
};

const AGENT_OPTIONS = [
  ["team", "Auto (orchestrateur)"],
  ["laravel", "Laravel"],
  ["react", "React / Inertia"],
  ["flutter", "Flutter"],
  ["devops", "DevOps"],
];

function StepNodeView({ data, type, selected }: NodeProps<StepNode>) {
  const kind = KINDS[type as NodeKind];
  const summary =
    type === "agent" ? `${data.agent ?? "team"} · ${(data.prompt ?? "{{input}}").slice(0, 60)}`
    : type === "command" ? data.command
    : type === "condition" ? `contient « ${data.contains ?? ""} » ?`
    : type === "patch" ? "Diff → branche agent/… (après approbation)"
    : "Texte saisi au lancement";
  return (
    <div className={`wf-node ${data.status ?? ""} ${selected ? "selected" : ""}`} style={{ borderTopColor: kind.color }}>
      {type !== "trigger" && <Handle type="target" position={Position.Left} />}
      <div className="wf-title" style={{ color: kind.color }}>{kind.title}</div>
      <div className="wf-label">{data.label}</div>
      <div className="wf-summary">{summary}</div>
      {data.waitingNote && <div className="wf-status waiting_approval">{data.waitingNote}</div>}
      {data.status && data.status !== "pending" && <div className={`wf-status ${data.status}`}>{STATUS_LABEL[data.status] ?? data.status}</div>}
      {type === "condition" ? (
        <>
          <Handle type="source" id="true" position={Position.Right} style={{ top: "35%" }} />
          <Handle type="source" id="false" position={Position.Right} style={{ top: "75%" }} />
          <span className="wf-port" style={{ top: "35%" }}>oui</span>
          <span className="wf-port" style={{ top: "75%" }}>non</span>
        </>
      ) : type === "command" ? (
        <>
          <Handle type="source" id="success" position={Position.Right} style={{ top: "35%" }} />
          <Handle type="source" id="failure" position={Position.Right} style={{ top: "75%" }} />
          <span className="wf-port" style={{ top: "35%" }}>succès</span>
          <span className="wf-port" style={{ top: "75%" }}>échec</span>
        </>
      ) : (
        <Handle type="source" position={Position.Right} />
      )}
    </div>
  );
}

const nodeTypes = { trigger: StepNodeView, agent: StepNodeView, command: StepNodeView, condition: StepNodeView, patch: StepNodeView };

const toFlow = (g: Graph): { nodes: StepNode[]; edges: Edge[] } => ({
  nodes: g.nodes.map((n) => ({ id: n.id, type: n.type, position: n.position, data: n.data as NodeData })),
  edges: g.edges.map((e, i) => ({ id: e.id ?? `e${i}`, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? undefined })),
});

const fromFlow = (nodes: StepNode[], edges: Edge[]): Graph => ({
  nodes: nodes.map((n) => {
    const { status: _status, ...data } = n.data; // l'état d'exécution n'est pas sauvegardé
    return { id: n.id, type: n.type as NodeKind, position: n.position, data };
  }),
  edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? null })),
});

const STARTER: Graph = {
  nodes: [
    { id: "n1", type: "trigger", position: { x: 0, y: 140 }, data: { label: "Demande" } },
    { id: "n2", type: "agent", position: { x: 250, y: 140 }, data: { label: "Rédige le correctif", agent: "team", prompt: "Analyse le problème ci-dessous, lis le code concerné, puis réponds UNIQUEMENT avec un diff au format `git diff` (lignes --- a/chemin, +++ b/chemin, hunks @@) dans un bloc ```diff, avec 3 lignes de contexte exactes.\n\nProblème :\n{{input}}" } },
    { id: "n3", type: "patch", position: { x: 500, y: 140 }, data: { label: "Appliquer sur une branche", source: "{{input}}", message: "Correctif proposé par un agent" } },
    { id: "n4", type: "command", position: { x: 760, y: 140 }, data: { label: "Tests dans la branche", command: "php artisan test", reason: "Vérifier le correctif", patch_node: "n3" } },
    { id: "n5", type: "agent", position: { x: 1040, y: 40 }, data: { label: "Bilan (OK)", agent: "team", prompt: "Les tests passent. Résume le correctif :\n{{n3}}\n\nSortie des tests :\n{{n4}}" } },
    { id: "n6", type: "agent", position: { x: 1040, y: 260 }, data: { label: "Analyse de l'échec", agent: "team", prompt: "Les tests échouent après ce correctif :\n{{n3}}\n\nSortie :\n{{n4}}\n\nExplique pourquoi." } },
  ],
  edges: [
    { source: "n1", target: "n2" },
    { source: "n2", target: "n3" },
    { source: "n3", target: "n4" },
    { source: "n4", target: "n5", sourceHandle: "success" },
    { source: "n4", target: "n6", sourceHandle: "failure" },
  ],
};

const API_ORIGIN = "http://127.0.0.1:8000";

function TriggerSettings({ workflowId, dirty }: { workflowId: string | null; dirty: boolean }) {
  const [cfg, setCfg] = useState<TriggerConfig | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setCfg(null);
    setMsg(null);
    setErr(null);
    if (workflowId) wfApi.getTrigger(workflowId).then(setCfg).catch((e) => setErr(e.message));
  }, [workflowId]);

  if (!workflowId) return <p className="hint">Enregistre le workflow pour configurer un lancement planifié ou par webhook.</p>;
  if (!cfg) return err ? <div className="error">{err}</div> : null;

  const set = (change: Partial<TriggerConfig>) => setCfg({ ...cfg, ...change });
  const save = async () => {
    setErr(null);
    setMsg(null);
    try {
      setCfg(await wfApi.saveTrigger(workflowId, { kind: cfg.kind, every_minutes: cfg.every_minutes, daily_at: cfg.daily_at, enabled: cfg.enabled, input: cfg.input }));
      setMsg("Déclencheur enregistré.");
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const url = cfg.webhook_path ? `${API_ORIGIN}${cfg.webhook_path}` : null;

  return (
    <div className="wf-trigger">
      <h4>Lancement automatique</h4>
      {dirty && <p className="hint">Modifications non enregistrées : le déclencheur s'applique à la dernière version enregistrée.</p>}
      <label>Mode
        <select value={cfg.kind} onChange={(e) => set({ kind: e.target.value as TriggerConfig["kind"], every_minutes: e.target.value === "interval" ? cfg.every_minutes ?? 60 : null, daily_at: e.target.value === "daily" ? cfg.daily_at ?? "09:00" : null })}>
          <option value="manual">Manuel uniquement</option>
          <option value="interval">Toutes les N minutes</option>
          <option value="daily">Chaque jour à une heure</option>
          <option value="webhook">Webhook (appel HTTP)</option>
        </select>
      </label>
      {cfg.kind === "interval" && <label>Minutes (5 à 10080)<input type="number" min={5} max={10080} value={cfg.every_minutes ?? 60} onChange={(e) => set({ every_minutes: Number(e.target.value) })} /></label>}
      {cfg.kind === "daily" && <label>Heure (locale)<input type="time" value={cfg.daily_at ?? "09:00"} onChange={(e) => set({ daily_at: e.target.value })} /></label>}
      {cfg.kind !== "manual" && (
        <>
          <label>Texte par défaut ({"{{trigger}}"})<textarea rows={3} value={cfg.input} onChange={(e) => set({ input: e.target.value })} maxLength={12000} /></label>
          <label className="check"><input type="checkbox" checked={cfg.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Activé</label>
        </>
      )}
      {url && (
        <div>
          <small>URL du webhook (POST, le corps devient le texte du déclencheur) :</small>
          <pre className="url">{url}</pre>
          <div className="actions">
            <button className="secondary" onClick={() => navigator.clipboard.writeText(url)}>Copier</button>
            <button className="secondary" onClick={async () => { if (window.confirm("Générer un nouveau jeton ? L'ancienne URL cessera de fonctionner.")) setCfg(await wfApi.rotateToken(workflowId)); }}>Nouveau jeton</button>
          </div>
          <p className="hint">Accessible uniquement depuis ce Mac (127.0.0.1). Pour GitHub, il faudrait un tunnel ; garde alors le jeton secret.</p>
        </div>
      )}
      {cfg.kind !== "manual" && cfg.last_run_at && <small>Dernier lancement : {cfg.last_run_at.replace("T", " ")}</small>}
      <button onClick={save}>Enregistrer le déclencheur</button>
      <p className="hint">Les commandes et correctifs demandent toujours ton approbation, même lancés automatiquement (30 min max).</p>
      {msg && <div className="ok">{msg}</div>}
      {err && <div className="error">{err}</div>}
    </div>
  );
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

function Editor({ openId, onOpened }: { openId: string | null; onOpened: () => void }) {
  const [list, setList] = useState<WorkflowSummary[]>([]);
  const [wfId, setWfId] = useState<string | null>(null);
  const [name, setName] = useState("Nouveau workflow");
  const [repo, setRepo] = useState("");
  const [nodes, setNodes] = useState<StepNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [input, setInput] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const refreshList = useCallback(() => wfApi.list().then(setList).catch((e) => setError(e.message)), []);
  useEffect(() => {
    refreshList();
  }, [refreshList]);

  const load = useCallback(async (id: string) => {
    setError(null);
    try {
      const wf: Workflow = await wfApi.get(id);
      const flow = toFlow(wf.graph);
      setWfId(wf.id);
      setName(wf.name);
      setRepo(wf.repo ?? "");
      setNodes(flow.nodes);
      setEdges(flow.edges);
      setSelected(null);
      setDirty(false);
      const past = await wfApi.runs(id);
      setRuns(past);
      setRun(past[0] ?? null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!openId) return;
    refreshList();
    load(openId).finally(onOpened);
  }, [openId, load, onOpened, refreshList]);

  const newWorkflow = (starter: boolean) => {
    const flow = toFlow(starter ? STARTER : { nodes: [{ id: "n1", type: "trigger", position: { x: 0, y: 100 }, data: { label: "Demande" } }], edges: [] });
    setWfId(null);
    setName(starter ? "Correctif → branche → tests" : "Nouveau workflow");
    setNodes(flow.nodes);
    setEdges(flow.edges);
    setSelected(null);
    setRun(null);
    setRuns([]);
    setDirty(true);
    setError(null);
  };

  // Suivi en direct de l'exécution
  useEffect(() => {
    if (!run || TERMINAL.has(run.status)) return;
    const timer = setInterval(async () => {
      try {
        const fresh = await wfApi.getRun(run.id);
        setRun(fresh);
        if (TERMINAL.has(fresh.status) && wfId) wfApi.runs(wfId).then(setRuns);
      } catch (e) {
        setError((e as Error).message);
      }
    }, 1200);
    return () => clearInterval(timer);
  }, [run, wfId]);

  const onNodesChange = useCallback((c: NodeChange<StepNode>[]) => {
    setNodes((n) => applyNodeChanges(c, n));
    if (c.some((x) => x.type !== "select" && x.type !== "dimensions")) setDirty(true);
    const sel = c.find((x) => x.type === "select");
    if (sel && sel.type === "select") setSelected(sel.selected ? sel.id : null);
  }, []);
  const onEdgesChange = useCallback((c: EdgeChange[]) => {
    setEdges((e) => applyEdgeChanges(c, e));
    if (c.some((x) => x.type !== "select")) setDirty(true);
  }, []);
  const onConnect = useCallback((conn: Connection) => {
    setEdges((e) => addEdge(conn, e));
    setDirty(true);
  }, []);

  const addNode = (kind: NodeKind) => {
    if (kind === "trigger" && nodes.some((n) => n.type === "trigger")) {
      setError("Un seul déclencheur par workflow.");
      return;
    }
    const id = `n${Date.now().toString(36)}`;
    const defaults: Record<NodeKind, NodeData> = {
      trigger: { label: "Demande" },
      agent: { label: "Agent", agent: "team", prompt: "{{input}}" },
      command: { label: "Commande", command: "git status", reason: "" },
      condition: { label: "Condition", contains: "" },
      patch: { label: "Appliquer le correctif", source: "{{input}}", message: "Correctif proposé par un agent" },
    };
    const x = nodes.length ? Math.max(...nodes.map((n) => n.position.x)) + 280 : 0;
    setNodes((n) => [...n, { id, type: kind, position: { x, y: 120 }, data: defaults[kind] }]);
    setSelected(id);
    setDirty(true);
  };

  const patch = (id: string, change: Partial<NodeData>) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...change } } : n)));
    setDirty(true);
  };

  const removeSelected = () => {
    if (!selected) return;
    setNodes((n) => n.filter((x) => x.id !== selected));
    setEdges((e) => e.filter((x) => x.source !== selected && x.target !== selected));
    setSelected(null);
    setDirty(true);
  };

  const save = async (): Promise<string | null> => {
    setError(null);
    setNote(null);
    try {
      const graph = fromFlow(nodes, edges);
      let id = wfId;
      if (id) await wfApi.update(id, name, repo, graph);
      else id = (await wfApi.create(name, repo, graph)).id;
      setWfId(id);
      setDirty(false);
      setNote("Workflow enregistré.");
      refreshList();
      return id;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  };

  const launch = async () => {
    const id = dirty || !wfId ? await save() : wfId;
    if (!id) return;
    setError(null);
    try {
      const { run_id } = await wfApi.run(id, input);
      setRun(await wfApi.getRun(run_id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async () => {
    if (!wfId || !window.confirm(`Supprimer « ${name} » et son historique ?`)) return;
    await wfApi.remove(wfId);
    newWorkflow(false);
    refreshList();
  };

  const decide = async (state: NodeState, action: "approve" | "reject") => {
    if (!state.proposal_id) return;
    if (action === "approve" && !window.confirm(`Exécuter cette commande ?\n\n${state.command}\n\nDans : ${repo || "REPO_ROOT"}${/test|phpunit/.test(state.command ?? "") ? "\n\n⚠ Les tests peuvent modifier la base si .env.testing n'est pas isolé." : ""}`)) return;
    try {
      await api[action](state.proposal_id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const decidePatch = async (nodeId: string, action: "approve" | "reject") => {
    if (!run) return;
    if (action === "approve" && !window.confirm("Appliquer ce correctif sur une nouvelle branche Git ?\n\nTon dossier de travail n'est pas modifié ; rien n'est poussé.")) return;
    try {
      await wfApi.decidePatch(run.id, nodeId, action);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const states = run?.states ?? {};
  const shown = useMemo(
    () => nodes.map((n) => ({ ...n, data: { ...n.data, status: states[n.id]?.status, waitingNote: states[n.id]?.waiting_note } })),
    [nodes, states],
  );
  const animatedEdges = useMemo(
    () => edges.map((e) => ({ ...e, animated: states[e.source]?.status === "succeeded" && states[e.target]?.status === "running" })),
    [edges, states],
  );
  const node = nodes.find((n) => n.id === selected);
  const nodeState = selected ? states[selected] : undefined;
  const running = run && !TERMINAL.has(run.status);

  return (
    <div className="wf-layout">
      <aside className="wf-list">
        <button onClick={() => newWorkflow(true)}>+ Depuis le modèle</button>
        <button className="secondary" onClick={() => newWorkflow(false)}>+ Vide</button>
        <ul>
          {list.map((w) => (
            <li key={w.id} className={w.id === wfId ? "active" : ""}>
              <button className="link" onClick={() => load(w.id)}>
                <strong>{w.name}</strong>
                <small>{w.updated_at.replace("T", " ")}</small>
              </button>
            </li>
          ))}
          {list.length === 0 && <li className="empty">Aucun workflow enregistré.</li>}
        </ul>
      </aside>

      <section className="wf-main">
        <div className="wf-bar">
          <input className="wf-name" value={name} onChange={(e) => { setName(e.target.value); setDirty(true); }} aria-label="Nom du workflow" />
          <input list="project-paths" value={repo} onChange={(e) => { setRepo(e.target.value); setDirty(true); }} placeholder="Dépôt (projet connecté ou chemin)" aria-label="Dépôt" />
          <button className="secondary" onClick={save} disabled={!dirty}>{dirty ? "Enregistrer" : "Enregistré"}</button>
          {wfId && <button className="secondary" onClick={remove}>Supprimer</button>}
        </div>

        <div className="wf-palette">
          {(Object.keys(KINDS) as NodeKind[]).map((k) => (
            <button key={k} className="secondary" title={KINDS[k].hint} onClick={() => addNode(k)}>
              <span className="dot" style={{ background: KINDS[k].color }} /> {KINDS[k].title}
            </button>
          ))}
          <span className="hint">Relie les nœuds en tirant d'un point à l'autre. Backspace supprime la sélection.</span>
        </div>

        <div className="wf-canvas">
          <ReactFlow
            nodes={shown}
            edges={animatedEdges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            fitView
            colorMode="system"
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        <div className="wf-run">
          <textarea value={input} onChange={(e) => setInput(e.target.value)} rows={2} placeholder="Laisse vide pour utiliser la « Tâche par défaut » du déclencheur, ou saisis un texte ({{trigger}})" maxLength={12000} />
          <div className="actions">
            <button onClick={launch} disabled={!!running || nodes.length === 0}>▶ Lancer</button>
            {running && <button className="secondary" onClick={() => wfApi.cancel(run!.id)}>Annuler</button>}
            {run && <span className={`badge ${run.status}`}>{run.status}</span>}
          </div>
        </div>
        {error && <div className="error">{error}</div>}
        {note && !error && <div className="ok">{note}</div>}
      </section>

      <aside className="wf-side">
        {node ? (
          <>
            <h3>{KINDS[node.type as NodeKind].title}</h3>
            <p className="hint">{KINDS[node.type as NodeKind].hint}</p>
            <label>Nom<input value={node.data.label} onChange={(e) => patch(node.id, { label: e.target.value })} /></label>
            {node.type === "agent" && (
              <>
                <label>Agent
                  <select value={node.data.agent ?? "team"} onChange={(e) => patch(node.id, { agent: e.target.value })}>
                    {AGENT_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
                <label>Prompt<textarea rows={7} value={node.data.prompt ?? ""} onChange={(e) => patch(node.id, { prompt: e.target.value })} /></label>
                <p className="hint">Variables : <code>{"{{input}}"}</code> (entrée), <code>{"{{trigger}}"}</code>, <code>{`{{${node.id}}}`}</code> = ce nœud.</p>
              </>
            )}
            {node.type === "command" && (
              <>
                <label>Commande<input value={node.data.command ?? ""} onChange={(e) => patch(node.id, { command: e.target.value })} /></label>
                <label>Raison<input value={node.data.reason ?? ""} onChange={(e) => patch(node.id, { reason: e.target.value })} /></label>
                <label>Exécuter dans la branche d'un patch
                  <select value={node.data.patch_node ?? ""} onChange={(e) => patch(node.id, { patch_node: e.target.value || undefined })}>
                    <option value="">Non — dépôt principal</option>
                    {nodes.filter((n) => n.type === "patch").map((n) => (
                      <option key={n.id} value={n.id}>{n.data.label}</option>
                    ))}
                  </select>
                </label>
                <p className="hint">Dans la branche, <code>vendor</code> et <code>node_modules</code> sont partagés mais pas <code>.env</code> : certains tests peuvent y échouer.</p>
                <p className="hint">Liste blanche uniquement (tests, lint, build, git lecture). Chaque exécution demande ton approbation.</p>
              </>
            )}
            {node.type === "condition" && (
              <label>Si l'entrée contient<input value={node.data.contains ?? ""} onChange={(e) => patch(node.id, { contains: e.target.value })} /></label>
            )}
            {node.type === "patch" && (
              <>
                <label>Texte contenant le diff<textarea rows={3} value={node.data.source ?? ""} onChange={(e) => patch(node.id, { source: e.target.value })} /></label>
                <label>Message de commit<input value={node.data.message ?? ""} onChange={(e) => patch(node.id, { message: e.target.value })} maxLength={200} /></label>
                <p className="hint">Attend un diff <code>git diff</code> (bloc <code>```diff</code>). Refusé : <code>.env</code>, <code>vendor</code>, <code>.git</code>, liens symboliques, plus de 20 fichiers.</p>
              </>
            )}
            {node.type === "trigger" && (
              <label>Tâche par défaut
                <textarea rows={6} value={node.data.input ?? ""} onChange={(e) => patch(node.id, { input: e.target.value })} maxLength={12000} />
                <small>Utilisée si le champ « Lancer » est vide (et par les lancements automatiques si aucun texte n'est défini).</small>
              </label>
            )}
            {node.type === "trigger" && <TriggerSettings workflowId={wfId} dirty={dirty} />}
            {node.type !== "trigger" && <button className="secondary" onClick={removeSelected}>Supprimer le nœud</button>}

            {nodeState && nodeState.status !== "pending" && (
              <div className="wf-result">
                <h4>Dernier résultat · {STATUS_LABEL[nodeState.status] ?? nodeState.status}</h4>
                {nodeState.status === "waiting_approval" && nodeState.kind === "patch" && (
                  <div className="card pending">
                    <strong>Correctif à valider</strong>
                    <small>{nodeState.files?.join(", ")}</small>
                    <pre className="diff">{nodeState.diff}</pre>
                    <div className="actions">
                      <button onClick={() => decidePatch(node.id, "approve")}>Appliquer sur une branche</button>
                      <button className="secondary" onClick={() => decidePatch(node.id, "reject")}>Refuser</button>
                    </div>
                  </div>
                )}
                {nodeState.status === "succeeded" && nodeState.git_branch && (
                  <div className="ok">
                    Branche <code>{nodeState.git_branch}</code>
                    <small> — dans le dépôt : <code>git diff HEAD...{nodeState.git_branch}</code></small>
                  </div>
                )}
                {nodeState.status === "waiting_approval" && nodeState.kind !== "patch" && (
                  <div className="card pending">
                    <code>{nodeState.command}</code>
                    <div className="actions">
                      <button onClick={() => decide(nodeState, "approve")}>Approuver</button>
                      <button className="secondary" onClick={() => decide(nodeState, "reject")}>Refuser</button>
                    </div>
                  </div>
                )}
                {nodeState.waiting_note && <div className="ok">{nodeState.waiting_note}</div>}
                {nodeState.error && <div className="error">{nodeState.error}</div>}
                {nodeState.exit_code != null && <small>Code de sortie : {nodeState.exit_code}</small>}
                {nodeState.output && <pre>{nodeState.output}</pre>}
              </div>
            )}
          </>
        ) : (
          <p className="empty">Sélectionne un nœud pour le configurer et voir son résultat.</p>
        )}

        {runs.length > 0 && (
          <div className="wf-history">
            <h4>Exécutions</h4>
            <ul>
              {runs.map((r) => (
                <li key={r.id} className={r.id === run?.id ? "active" : ""}>
                  <button className="link" onClick={() => setRun(r)}>
                    {r.created_at.replace("T", " ")} <span className={`badge ${r.status}`}>{r.status}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </aside>
    </div>
  );
}

export default function WorkflowsPanel({ openId, onOpened }: { openId: string | null; onOpened: () => void }) {
  return (
    <ReactFlowProvider>
      <Editor openId={openId} onOpened={onOpened} />
    </ReactFlowProvider>
  );
}
