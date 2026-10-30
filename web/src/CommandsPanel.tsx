import { useCallback, useEffect, useState } from "react";
import { api, Proposal } from "./api";

const RISKY = /\b(test|phpunit)\b/;

export default function CommandsPanel({ refreshKey, onCount }: { refreshKey: number; onCount: (n: number) => void }) {
  const [items, setItems] = useState<Proposal[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const all = await api.commands();
      setItems(all.slice().reverse());
      onCount(all.filter((p) => p.status === "pending").length);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [onCount]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  // Les workflows créent des propositions en arrière-plan : on rafraîchit régulièrement.
  useEffect(() => {
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [load]);

  const act = async (p: Proposal, action: "approve" | "reject") => {
    if (action === "approve") {
      const warn = RISKY.test(p.command)
        ? "\n\n⚠ Les tests peuvent vider ou modifier la base si .env.testing n'est pas isolé."
        : "";
      if (!window.confirm(`Exécuter cette commande ?\n\n${p.command}\n\nDans : ${p.repo}${warn}`)) return;
    }
    setBusyId(p.id);
    setError(null);
    try {
      await api[action](p.id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const copy = (p: Proposal) => navigator.clipboard.writeText(`$ ${p.command}\n(code ${p.exit_code})\n${p.output ?? ""}`);

  return (
    <div className="panel">
      <h2>Commandes proposées par les agents</h2>
      <p className="hint">
        Rien ne s'exécute sans ton approbation. Après exécution, copie la sortie et colle-la dans le chat : l'agent ne la voit pas.
      </p>
      {error && <div className="error">{error}</div>}
      {items.length === 0 && <p className="empty">Aucune proposition.</p>}
      {items.map((p) => (
        <div key={p.id} className={`card ${p.status}`}>
          <div className="row">
            <code className="cmd">{p.command}</code>
            <span className={`badge ${p.status}`}>{p.status}</span>
          </div>
          <small>
            {p.repo} · {p.created_at.replace("T", " ")}
          </small>
          {p.reason && <p>{p.reason}</p>}
          {p.status === "pending" && (
            <div className="actions">
              <button disabled={busyId === p.id} onClick={() => act(p, "approve")}>
                Approuver et exécuter
              </button>
              <button className="secondary" disabled={busyId === p.id} onClick={() => act(p, "reject")}>
                Refuser
              </button>
            </div>
          )}
          {p.status === "executed" && (
            <>
              <div className="row">
                <strong>Code de sortie : {p.exit_code}</strong>
                <button className="secondary" onClick={() => copy(p)}>
                  Copier la sortie
                </button>
              </div>
              <pre>{p.output || "(aucune sortie)"}</pre>
            </>
          )}
        </div>
      ))}
    </div>
  );
}
