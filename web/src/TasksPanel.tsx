import { useEffect, useState } from "react";
import { ConnectedProject, projectApi, WorkflowProposal } from "./api";
import { StackBadges } from "./ProjectsPanel";

const EXAMPLES = [
  "Corrige l'erreur SQLSTATE 42P01 sur droits_paiement dans DossierPaiementDmgController",
  "Ajoute un filtre par date sur la liste des dossiers",
  "Fais un audit de sécurité des uploads de fichiers",
  "Revue de code du service de paiement",
];

export default function TasksPanel({ projects, onOpenWorkflow, onGoProjects }: {
  projects: ConnectedProject[];
  onOpenWorkflow: (workflowId: string) => void;
  onGoProjects: () => void;
}) {
  const [projectId, setProjectId] = useState("");
  const [task, setTask] = useState("");
  const [proposals, setProposals] = useState<WorkflowProposal[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId && projects.length) setProjectId(projects[0].id);
    if (projectId && !projects.some((p) => p.id === projectId)) setProjectId(projects[0]?.id ?? "");
  }, [projects, projectId]);

  const project = projects.find((p) => p.id === projectId);

  const propose = async () => {
    if (!project) return;
    setBusy("propose");
    setError(null);
    try {
      setProposals((await projectApi.propose(project.id, task.trim())).proposals);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const create = async (template: string, launch: boolean) => {
    if (!project) return;
    setBusy(template + launch);
    setError(null);
    try {
      const res = await projectApi.create(project.id, task.trim(), template, launch);
      onOpenWorkflow(res.workflow_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (projects.length === 0) {
    return (
      <div className="panel">
        <h2>Nouvelle tâche</h2>
        <p className="empty">Connecte d'abord un projet pour décrire une tâche.</p>
        <button onClick={onGoProjects}>Détecter mes projets</button>
      </div>
    );
  }

  return (
    <div className="panel wide">
      <h2>Nouvelle tâche</h2>
      <p className="hint">Décris ce que tu veux faire : je propose des workflows adaptés à la pile du projet. Tu choisis, tu relis, puis tu crées. Rien ne s'exécute sans ton approbation.</p>
      {error && <div className="error">{error}</div>}

      <div className="card">
        <label>Projet
          <select value={projectId} onChange={(e) => { setProjectId(e.target.value); setProposals(null); }}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
        {project && (
          <div className="chips">
            <StackBadges stacks={project.stacks} />
            <small>{project.check_command ? <>vérification : <code>{project.check_command}</code></> : "aucune commande de vérification connue"}</small>
          </div>
        )}
        <label>Tâche
          <textarea rows={4} value={task} onChange={(e) => { setTask(e.target.value); setProposals(null); }} maxLength={4000}
            placeholder="Ex. : Corrige l'erreur SQL sur la page des paiements (colle ici le message d'erreur ou le log)" />
        </label>
        <div className="chips">
          {EXAMPLES.map((ex) => <button key={ex} className="chip action" onClick={() => { setTask(ex); setProposals(null); }}>{ex.length > 48 ? ex.slice(0, 46) + "…" : ex}</button>)}
        </div>
        <div className="actions">
          <button onClick={propose} disabled={!project || task.trim().length < 3 || busy === "propose"}>
            {busy === "propose" ? "Analyse…" : "Proposer des workflows"}
          </button>
        </div>
      </div>

      {proposals && (
        <>
          <h3 className="eyebrow">Propositions</h3>
          <div className="grid">
            {proposals.map((p) => (
              <div key={p.template} className={`card proposal ${p.recommended ? "recommended" : ""}`}>
                <div className="row">
                  <strong>{p.title}</strong>
                  {p.recommended && <span className="chip fill">Recommandé</span>}
                </div>
                <p className="summary">{p.summary}</p>
                <ol className="steps">{p.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                <div className="chips">
                  <span className="chip">agent : {p.agent}</span>
                  <span className={`chip ${p.writes_code ? "warn" : ""}`}>{p.writes_code ? "écrit du code (branche dédiée)" : "lecture seule"}</span>
                  {p.approvals > 0 && <span className="chip">{p.approvals} approbation(s)</span>}
                </div>
                <div className="actions">
                  <button onClick={() => create(p.template, true)} disabled={busy !== null}>{busy === p.template + "true" ? "Création…" : "Créer et lancer"}</button>
                  <button className="secondary" onClick={() => create(p.template, false)} disabled={busy !== null}>Créer seulement</button>
                </div>
              </div>
            ))}
          </div>
          <p className="hint">« Créer » ouvre le workflow dans l'éditeur : tu peux ajuster chaque nœud avant de le lancer.</p>
        </>
      )}
    </div>
  );
}
