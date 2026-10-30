import { useEffect, useMemo, useState } from "react";
import { ConnectedProject, ProjectInfo, projectApi } from "./api";

export function StackBadges({ stacks }: { stacks: string[] }) {
  if (stacks.length === 0) return <span className="chip muted">non reconnu</span>;
  return (
    <>
      {stacks.map((s) => (
        <span key={s} className="chip">
          {s}
        </span>
      ))}
    </>
  );
}

export default function ProjectsPanel({ projects, onChange }: { projects: ConnectedProject[]; onChange: () => void }) {
  const [roots, setRoots] = useState("");
  const [found, setFound] = useState<ProjectInfo[] | null>(null);
  const [scannedRoots, setScannedRoots] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [manual, setManual] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connectedPaths = useMemo(() => new Set(projects.map((p) => p.path)), [projects]);

  const detect = async () => {
    setBusy(true);
    setError(null);
    try {
      const list = roots.split("\n").map((r) => r.trim()).filter(Boolean);
      const res = await projectApi.scan(list.length ? list : null);
      setFound(res.projects);
      setScannedRoots(res.roots);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    detect();
    // détection unique à l'ouverture de l'onglet
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connect = async (path: string) => {
    setError(null);
    try {
      await projectApi.connect(path);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const disconnect = async (p: ConnectedProject) => {
    if (!window.confirm(`Retirer « ${p.name} » de la liste ? Le dépôt n'est pas touché.`)) return;
    await projectApi.disconnect(p.id);
    onChange();
  };

  const shown = (found ?? []).filter((p) => !connectedPaths.has(p.path) && `${p.name} ${p.stacks.join(" ")}`.toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="panel wide">
      <h2>Projets</h2>
      <p className="hint">
        La détection est en lecture seule : elle cherche des dépôts Git et lit leurs fichiers de configuration. Seuls les projets que tu connectes sont proposés dans les tâches et les workflows.
      </p>
      {error && <div className="error">{error}</div>}

      <h3 className="eyebrow">Connectés ({projects.length})</h3>
      {projects.length === 0 && <p className="empty">Aucun projet connecté. Connecte-en un depuis la détection ci-dessous.</p>}
      <div className="grid">
        {projects.map((p) => (
          <div key={p.id} className="card connected">
            <div className="row">
              <strong>{p.name}</strong>
              <button className="secondary small" onClick={() => disconnect(p)}>Retirer</button>
            </div>
            <div className="chips"><StackBadges stacks={p.stacks} /></div>
            <small className="path">{p.path}</small>
            <small>Vérification : {p.check_command ? <code>{p.check_command}</code> : "aucune commande connue"}</small>
          </div>
        ))}
      </div>

      <h3 className="eyebrow">Détecter</h3>
      <div className="card">
        <label>
          Dossiers à explorer (un par ligne, vide = dossiers par défaut du serveur)
          <textarea rows={2} value={roots} onChange={(e) => setRoots(e.target.value)} placeholder="/Applications/MAMP/htdocs" />
        </label>
        <div className="row">
          <div className="actions">
            <button onClick={detect} disabled={busy}>{busy ? "Détection…" : "Détecter les projets"}</button>
          </div>
          {scannedRoots.length > 0 && <small>Explorés : {scannedRoots.join(", ")} (2 niveaux)</small>}
        </div>
        <div className="row">
          <input value={manual} onChange={(e) => setManual(e.target.value)} placeholder="Ou un chemin précis : /Users/…/mon-projet" />
          <button className="secondary" disabled={!manual.trim()} onClick={() => connect(manual.trim()).then(() => setManual(""))}>Connecter ce chemin</button>
        </div>
      </div>

      {found && (
        <>
          <div className="row">
            <h3 className="eyebrow">{shown.length} projet(s) détecté(s) non connectés</h3>
            <input className="filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filtrer (nom, laravel, flutter…)" />
          </div>
          <div className="grid">
            {shown.map((p) => (
              <div key={p.path} className="card">
                <div className="row">
                  <strong>{p.name}</strong>
                  <button className="small" onClick={() => connect(p.path)}>Connecter</button>
                </div>
                <div className="chips"><StackBadges stacks={p.stacks} /></div>
                <small className="path">{p.path}</small>
                <small>
                  {p.branch ?? "?"}
                  {p.dirty_files ? ` · ${p.dirty_files} fichier(s) modifié(s)` : " · propre"}
                  {p.last_commit ? ` · ${p.last_commit}` : ""}
                </small>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
