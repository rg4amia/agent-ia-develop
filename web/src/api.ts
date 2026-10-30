export type AgentKey = "team" | "laravel" | "react" | "flutter" | "devops";

export interface ChatResult {
  agent: string;
  model: string;
  answer: string;
  tools_used: string[];
  conversation_id: string;
}

export interface ConversationSummary {
  id: string;
  agent: string;
  repo: string | null;
  created_at: string;
  messages: number;
}

export interface StoredMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface Proposal {
  id: string;
  repo: string;
  command: string;
  reason: string;
  status: "pending" | "rejected" | "executed";
  created_at: string;
  exit_code: number | null;
  output: string | null;
}

export interface KnowledgeItem {
  id: number;
  title: string;
  source: string | null;
  created_at: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* réponse non JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  chat: (agent: AgentKey, message: string, repo: string, conversationId: string | null) =>
    request<ChatResult>(agent === "team" ? "/team/chat" : `/agents/${agent}/chat`, post({
      message,
      repo: repo || null,
      conversation_id: conversationId,
    })),
  conversations: () => request<ConversationSummary[]>("/conversations"),
  conversation: (id: string) => request<StoredMessage[]>(`/conversations/${id}`),
  deleteConversation: (id: string) => request<unknown>(`/conversations/${id}`, { method: "DELETE" }),

  commands: () => request<Proposal[]>("/commands"),
  approve: (id: string) => request<Proposal>(`/commands/${id}/approve`, { method: "POST" }),
  reject: (id: string) => request<Proposal>(`/commands/${id}/reject`, { method: "POST" }),

  knowledge: () => request<KnowledgeItem[]>("/knowledge"),
  addKnowledge: (title: string, content: string) => request<{ fragments: number }>("/knowledge", post({ title, content })),
  importKnowledge: (path: string, repo: string) =>
    request<{ fragments: number }>("/knowledge/import", post({ path, repo: repo || null })),
  deleteKnowledge: (id: number) => request<unknown>(`/knowledge/${id}`, { method: "DELETE" }),
};

export type NodeKind = "trigger" | "agent" | "command" | "condition" | "patch";
export type NodeStatus = "pending" | "running" | "waiting_approval" | "succeeded" | "failed" | "skipped";

export interface GraphNode {
  id: string;
  type: NodeKind;
  position: { x: number; y: number };
  data: Record<string, unknown>;
}
export interface GraphEdge {
  id?: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
}
export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}
export interface WorkflowSummary {
  id: string;
  name: string;
  repo: string | null;
  updated_at: string;
}
export interface Workflow extends WorkflowSummary {
  graph: Graph;
}
export interface NodeState {
  status: NodeStatus;
  output?: string;
  error?: string;
  proposal_id?: string;
  command?: string;
  exit_code?: number | null;
  branch?: string;
  tools?: string[];
  kind?: "command" | "patch";
  waiting_note?: string;
  diff?: string;
  files?: string[];
  git_branch?: string;
  worktree?: string;
  stat?: string;
}

export interface TriggerConfig {
  kind: "manual" | "interval" | "daily" | "webhook";
  every_minutes: number | null;
  daily_at: string | null;
  enabled: boolean;
  input: string;
  last_run_at: string | null;
  webhook_path: string | null;
}
export interface Run {
  id: string;
  workflow_id: string;
  status: "running" | "waiting" | "succeeded" | "failed" | "cancelled";
  input: string;
  states: Record<string, NodeState>;
  created_at: string;
  finished_at: string | null;
}

export const wfApi = {
  list: () => request<WorkflowSummary[]>("/workflows"),
  get: (id: string) => request<Workflow>(`/workflows/${id}`),
  create: (name: string, repo: string, graph: Graph) =>
    request<{ id: string }>("/workflows", post({ name, repo: repo || null, graph })),
  update: (id: string, name: string, repo: string, graph: Graph) =>
    request<{ id: string }>(`/workflows/${id}`, { method: "PUT", body: JSON.stringify({ name, repo: repo || null, graph }) }),
  remove: (id: string) => request<unknown>(`/workflows/${id}`, { method: "DELETE" }),
  run: (id: string, input: string) => request<{ run_id: string }>(`/workflows/${id}/run`, post({ input })),
  runs: (id: string) => request<Run[]>(`/runs?workflow_id=${id}`),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  getTrigger: (id: string) => request<TriggerConfig>(`/workflows/${id}/trigger`),
  saveTrigger: (id: string, t: Pick<TriggerConfig, "kind" | "every_minutes" | "daily_at" | "enabled" | "input">) =>
    request<TriggerConfig>(`/workflows/${id}/trigger`, { method: "PUT", body: JSON.stringify(t) }),
  rotateToken: (id: string) => request<TriggerConfig>(`/workflows/${id}/trigger/rotate`, { method: "POST" }),
  decidePatch: (runId: string, nodeId: string, action: "approve" | "reject") =>
    request<unknown>(`/runs/${runId}/nodes/${nodeId}/${action}`, { method: "POST" }),
  cancel: (id: string) => request<unknown>(`/runs/${id}/cancel`, { method: "POST" }),
};

export interface ProjectInfo {
  id?: string;
  name: string;
  path: string;
  stacks: string[];
  check_command: string | null;
  branch?: string | null;
  dirty_files?: number;
  last_commit?: string | null;
  connected?: boolean;
}
export interface ConnectedProject extends ProjectInfo {
  id: string;
}

export interface WorkflowProposal {
  template: string;
  title: string;
  summary: string;
  agent: string;
  steps: string[];
  writes_code: boolean;
  approvals: number;
  score: number;
  recommended: boolean;
}

export const projectApi = {
  list: () => request<ConnectedProject[]>("/projects"),
  scan: (roots: string[] | null) =>
    request<{ roots: string[]; projects: ProjectInfo[] }>("/projects/scan", post({ roots })),
  connect: (path: string) => request<ConnectedProject>("/projects", post({ path })),
  disconnect: (id: string) => request<unknown>(`/projects/${id}`, { method: "DELETE" }),
  propose: (projectId: string, task: string) =>
    request<{ project: ConnectedProject; proposals: WorkflowProposal[] }>("/planner/propose", post({ project_id: projectId, task })),
  create: (projectId: string, task: string, template: string, launch: boolean) =>
    request<{ workflow_id: string; run_id: string | null }>("/planner/create", post({ project_id: projectId, task, template, launch })),
};
