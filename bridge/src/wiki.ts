// Measured against a live wiki: hybrid search takes ~3s (embed + rerank), so
// 1500 ms made the piggyback silently skip every time. Keep it short enough
// to never stall a code answer badly, long enough to actually land.
const DEFAULT_TIMEOUT_MS = 5000;
const ASK_TIMEOUT_MS = 120_000;
// create_exogenous jobs run enrichment synchronously and can take ~2 min.
const SAVE_TIMEOUT_MS = 180_000;
const POLL_INTERVAL_MS = 500;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function extractNodeId(result: unknown): string | null {
  if (!result || typeof result !== 'object') return null;
  const maybeResult = result as {
    id?: unknown;
    node?: { id?: unknown };
  };
  if (typeof maybeResult.id === 'string' && maybeResult.id) return maybeResult.id;
  if (maybeResult.node && typeof maybeResult.node.id === 'string' && maybeResult.node.id) {
    return maybeResult.node.id;
  }
  return null;
}

export interface WikiNote {
  id: string;
  title: string;
  summary: string;
  body: string;
  created_at: string;
}

export class WikiClient {
  constructor(private baseUrl: string, private timeoutMs: number) {}

  private async req<T = unknown>(
    path: string,
    init: RequestInit = {},
    timeoutMs = this.timeoutMs,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const url = new URL(path, this.baseUrl);
      const headers = new Headers(init.headers);
      if (init.body !== undefined && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }
      const res = await fetch(url, {
        ...init,
        headers,
        signal: controller.signal,
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => res.statusText);
        throw new Error(`${res.status}: ${detail}`);
      }
      if (res.status === 204) {
        return null as T;
      }
      return (await res.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  // Never throws. Piggyback search must not break code answers.
  async search(q: string, limit = 5): Promise<WikiNote[]> {
    const query = q.trim();
    if (!query) return [];
    try {
      const notes = await this.req<WikiNote[]>(
        `/api/search?q=${encodeURIComponent(query)}&limit=${limit}`,
      );
      return Array.isArray(notes) ? notes : [];
    } catch (err) {
      console.error(`wiki search skipped: ${formatError(err)}`);
      return [];
    }
  }

  async ingestRaw(text: string, origin: string | null): Promise<void> {
    try {
      await this.req(
        '/api/ingest-raw',
        {
          method: 'POST',
          body: JSON.stringify({ text, origin }),
        },
        10_000,
      );
    } catch (err) {
      console.error(`wiki ingest_raw skipped: ${formatError(err)}`);
    }
  }

  async ask(question: string): Promise<string> {
    const data = await this.req<any>(
      '/api/ask',
      { method: 'POST', body: JSON.stringify({ question, overrides: null }) },
      ASK_TIMEOUT_MS,
    );

    const answer = data?.answer;
    if (typeof answer === 'string') return answer;
    if (answer && typeof answer === 'object') {
      if (typeof answer.text === 'string') return answer.text;
      if (typeof answer.answer === 'string') return answer.answer;
      return JSON.stringify(answer, null, 2);
    }
    return JSON.stringify(data, null, 2);
  }

  async saveNote(body: string, origin: string, question: string | null): Promise<string> {
    const job = await this.req<any>('/api/exogenous', {
      method: 'POST',
      body: JSON.stringify({
        body,
        source_node_ids: [],
        origin,
        question,
      }),
    });

    const jobId = typeof job?.id === 'string' ? job.id : null;
    if (!jobId) {
      throw new Error('create_exogenous がジョブIDを返しませんでした');
    }

    let current = job;
    const started = Date.now();

    while (current?.status === 'queued' || current?.status === 'running') {
      if (Date.now() - started > SAVE_TIMEOUT_MS) {
        throw new Error(`書き込みジョブがタイムアウトしました: ${current.type ?? 'create_exogenous'}`);
      }
      await sleep(POLL_INTERVAL_MS);
      current = await this.req<any>(`/api/write-jobs/${encodeURIComponent(jobId)}`);
    }

    if (current?.status === 'failed') {
      throw new Error(current.error || `書き込みジョブが失敗しました: ${current.type ?? 'create_exogenous'}`);
    }
    if (current?.status === 'cancelled') {
      throw new Error(
        `書き込みジョブがキャンセルされました: ${current.type ?? 'create_exogenous'}`,
      );
    }

    const nodeId = extractNodeId(current?.result);
    if (!nodeId) {
      throw new Error('書き込みジョブは完了しましたが、ノードIDがありません');
    }
    return nodeId;
  }
}

export const DEFAULT_WIKI_TIMEOUT_MS = DEFAULT_TIMEOUT_MS;
