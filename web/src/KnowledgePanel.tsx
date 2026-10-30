import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, KnowledgeItem } from "./api";

export default function KnowledgePanel() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [path, setPath] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => api.knowledge().then(setItems).catch((e) => setError(e.message)), []);
  useEffect(() => {
    load();
  }, [load]);

  const run = async (job: () => Promise<{ fragments: number }>) => {
    setError(null);
    setMessage(null);
    try {
      const r = await job();
      setMessage(`${r.fragments} fragment(s) indexé(s).`);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const addText = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.addKnowledge(title.trim(), content.trim())).then(() => {
      setTitle("");
      setContent("");
    });
  };

  const importFile = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.importKnowledge(path.trim(), localStorage.getItem("repo") ?? "")).then(() => setPath(""));
  };

  const remove = async (id: number) => {
    await api.deleteKnowledge(id);
    load();
  };

  return (
    <div className="panel">
      <h2>Base documentaire</h2>
      <p className="hint">Les agents y cherchent conventions, décisions et notes d'architecture (outil search_docs).</p>
      {message && <div className="ok">{message}</div>}
      {error && <div className="error">{error}</div>}

      <form onSubmit={importFile} className="card">
        <h3>Importer un fichier du dépôt</h3>
        <div className="row">
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="docs/architecture.md (.md, .txt, .rst)" required />
          <button type="submit">Importer</button>
        </div>
      </form>

      <form onSubmit={addText} className="card">
        <h3>Ajouter une note</h3>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Titre" required maxLength={200} />
        <textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="Contenu" rows={5} required />
        <button type="submit">Ajouter</button>
      </form>

      <h3>Documents indexés ({items.length})</h3>
      {items.length === 0 && <p className="empty">Aucun document.</p>}
      <ul className="docs">
        {items.map((d) => (
          <li key={d.id}>
            <span>
              {d.title} <small>{d.source ?? "note manuelle"}</small>
            </span>
            <button className="icon" title="Supprimer" onClick={() => remove(d.id)}>
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
