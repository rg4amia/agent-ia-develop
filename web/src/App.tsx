import { useCallback, useEffect, useState } from "react";
import { ConnectedProject, projectApi } from "./api";
import ProjectsPanel from "./ProjectsPanel";
import TasksPanel from "./TasksPanel";
import ChatPanel from "./ChatPanel";
import CommandsPanel from "./CommandsPanel";
import KnowledgePanel from "./KnowledgePanel";
import WorkflowsPanel from "./WorkflowsPanel";

type Tab = "tasks" | "projects" | "chat" | "workflows" | "commands" | "knowledge";

export default function App() {
  const [tab, setTab] = useState<Tab>("tasks");
  const [projects, setProjects] = useState<ConnectedProject[]>([]);
  const [openWorkflow, setOpenWorkflow] = useState<string | null>(null);
  const loadProjects = useCallback(() => projectApi.list().then(setProjects).catch(() => undefined), []);
  useEffect(() => {
    loadProjects();
  }, [loadProjects]);
  const [pending, setPending] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const onCount = useCallback((n: number) => setPending(n), []);

  return (
    <div className="app">
      <header>
        <h1>AEJ Agents</h1>
        <nav>
          <button className={tab === "tasks" ? "on" : ""} onClick={() => setTab("tasks")}>
            Tâches
          </button>
          <button className={tab === "projects" ? "on" : ""} onClick={() => setTab("projects")}>
            Projets{projects.length > 0 && <span className="count neutral">{projects.length}</span>}
          </button>
          <button className={tab === "chat" ? "on" : ""} onClick={() => setTab("chat")}>
            Chat
          </button>
          <button className={tab === "workflows" ? "on" : ""} onClick={() => setTab("workflows")}>
            Workflows
          </button>
          <button className={tab === "commands" ? "on" : ""} onClick={() => setTab("commands")}>
            Commandes{pending > 0 && <span className="count">{pending}</span>}
          </button>
          <button className={tab === "knowledge" ? "on" : ""} onClick={() => setTab("knowledge")}>
            Documentation
          </button>
        </nav>
      </header>
      <datalist id="project-paths">
        {projects.map((p) => <option key={p.id} value={p.path}>{p.name}</option>)}
      </datalist>
      <main>
        {tab === "tasks" && (
          <TasksPanel
            projects={projects}
            onGoProjects={() => setTab("projects")}
            onOpenWorkflow={(id) => { setOpenWorkflow(id); setTab("workflows"); }}
          />
        )}
        {tab === "projects" && <ProjectsPanel projects={projects} onChange={loadProjects} />}
        {/* Le chat reste monté pour conserver la conversation en cours quand on change d'onglet. */}
        <div hidden={tab !== "chat"}>
          <ChatPanel onProposalsMayHaveChanged={() => setRefreshKey((k) => k + 1)} />
        </div>
        {/* Monté en permanence : une exécution en cours continue d'être suivie hors de l'onglet. */}
        <div hidden={tab !== "workflows"}>
          <WorkflowsPanel openId={openWorkflow} onOpened={() => setOpenWorkflow(null)} />
        </div>
        {/* Monté même masqué pour tenir le compteur de commandes à jour. */}
        <div hidden={tab !== "commands"}>
          <CommandsPanel refreshKey={refreshKey} onCount={onCount} />
        </div>
        {tab === "knowledge" && <KnowledgePanel />}
      </main>
    </div>
  );
}
