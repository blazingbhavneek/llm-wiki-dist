import type { WikiClient } from './wiki';

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function extractQuery(args: Record<string, unknown>): string {
  if (typeof args.query === 'string') return args.query;
  if (typeof args.title === 'string') return args.title;
  if (typeof args.question === 'string') return args.question;
  return '';
}

export class TraceBuffer {
  private entries: string[] = [];
  private interval: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(
    private readonly wiki: WikiClient,
    private readonly origin: string,
    private readonly flushMs: number,
  ) {}

  record(name: string, args: Record<string, unknown>, resultText: string): void {
    try {
      const query = extractQuery(args);
      const entry = `[${new Date().toISOString()}] TOOL ${name} QUERY ${query}\nRESULT ${resultText.slice(
        0,
        500,
      )}`;
      this.entries.push(entry);
    } catch (err) {
      console.error(`trace record skipped: ${formatError(err)}`);
    }
  }

  flush(): Promise<void> {
    return this.flushEntries(true);
  }

  start(): NodeJS.Timeout {
    if (this.interval) return this.interval;

    this.stopped = false;
    this.interval = setInterval(() => {
      void this.flushEntries(false);
    }, this.flushMs);
    this.interval.unref();
    return this.interval;
  }

  stop(): Promise<void> {
    if (this.stopped) return Promise.resolve();

    this.stopped = true;
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
    return this.flush();
  }

  private flushEntries(force: boolean): Promise<void> {
    if (this.entries.length === 0) return Promise.resolve();
    if (!force && this.entries.length < 3) return Promise.resolve();

    const joined = this.entries.join('\n\n');
    this.entries = [];

    return this.wiki.ingestRaw(joined, this.origin).catch((err) => {
      console.error(`trace ingest skipped: ${formatError(err)}`);
    });
  }
}
