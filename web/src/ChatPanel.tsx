import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AgentKey, api, ConversationSummary, StoredMessage } from "./api";

// Liens ouverts dans un nouvel onglet, sans accès à la fenêtre d'origine.
function SafeLink({ href, children }: { href?: string; children?: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  );
}

const AGENTS: { key: AgentKey; label: string }[] = [
  { key: "team", label: "Auto (orchestrateur)" },
  { key: "laravel", label: "Laravel" },
  { key: "react", label: "React / Inertia" },
  { key: "flutter", label: "Flutter" },
  { key: "devops", label: "DevOps" },
];

interface Turn extends Pick<StoredMessage, "role" | "content"> {
  meta?: string;
  tools?: string[];
}

export default function ChatPanel({ onProposalsMayHaveChanged }: { onProposalsMayHaveChanged: () => void }) {
  const [agent, setAgent] = useState<AgentKey>("team");
  const [repo, setRepo] = useState(() => localStorage.getItem("repo") ?? "");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const refreshHistory = () => api.conversations().then(setHistory).catch(() => undefined);

  useEffect(() => {
    refreshHistory();
  }, []);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);
  useEffect(() => {
    localStorage.setItem("repo", repo);
  }, [repo]);

  const open = async (id: string) => {
    setError(null);
    try {
      const messages = await api.conversation(id);
      setTurns(messages.map(({ role, content }) => ({ role, content })));
      setConversationId(id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reset = () => {
    setConversationId(null);
    setTurns([]);
    setError(null);
  };

  const remove = async (id: string) => {
    if (!window.confirm("Supprimer cette conversation ?")) return;
    await api.deleteConversation(id);
    if (id === conversationId) reset();
    refreshHistory();
  };

  const send = async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setError(null);
    setBusy(true);
    setTurns((t) => [...t, { role: "user", content: message }]);
    try {
      const res = await api.chat(agent, message, repo.trim(), conversationId);
      setConversationId(res.conversation_id);
      setTurns((t) => [
        ...t,
        { role: "assistant", content: res.answer, meta: `${res.agent} · ${res.model}`, tools: res.tools_used },
      ]);
      refreshHistory();
      if (res.tools_used.some((u) => u.startsWith("propose_command"))) onProposalsMayHaveChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-layout">
      <aside className="history">
        <button onClick={reset}>+ Nouvelle conversation</button>
        <ul>
          {history.map((c) => (
            <li key={c.id} className={c.id === conversationId ? "active" : ""}>
              <button className="link" onClick={() => open(c.id)}>
                <strong>{c.agent.replace("AEJ ", "")}</strong>
                <small>
                  {c.created_at.replace("T", " ")} · {c.messages} msg
                </small>
              </button>
              <button className="icon" title="Supprimer" onClick={() => remove(c.id)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="chat">
        <div className="toolbar">
          <label>
            Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value as AgentKey)}>
              {AGENTS.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>
          <label className="grow">
            Dépôt
            <input
              list="project-paths"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="vide = REPO_ROOT du serveur"
            />
          </label>
        </div>

        <div className="messages">
          {turns.length === 0 && <p className="empty">Pose une question sur le code du dépôt. Les agents lisent, ils ne modifient rien.</p>}
          {turns.map((t, i) => (
            <div key={i} className={`bubble ${t.role}`}>
              {t.meta && <div className="meta">{t.meta}</div>}
              {t.role === "assistant" ? (
                // Pas de rehype-raw : le HTML brut du modèle n'est jamais interprété.
                <div className="text markdown">
                  <Markdown remarkPlugins={[remarkGfm]} components={{ a: SafeLink }}>
                    {t.content}
                  </Markdown>
                </div>
              ) : (
                <div className="text">{t.content}</div>
              )}
              {t.tools && t.tools.length > 0 && (
                <details>
                  <summary>{t.tools.length} appel(s) d'outil</summary>
                  <ul>
                    {t.tools.map((u, j) => (
                      <li key={j}>
                        <code>{u}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          ))}
          {busy && <div className="bubble assistant pending">L'agent analyse le dépôt…</div>}
          {error && <div className="error">{error}</div>}
          <div ref={bottom} />
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
            }}
            placeholder="Ex. Analyse app/Http/Controllers/... et cherche des bugs (⌘/Ctrl + Entrée pour envoyer)"
            rows={3}
            maxLength={12000}
          />
          <button type="submit" disabled={busy || !input.trim()}>
            Envoyer
          </button>
        </form>
      </section>
    </div>
  );
}
