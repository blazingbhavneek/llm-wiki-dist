import os from 'os';
import path from 'path';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { openIndex } from './codegraph';
import { createBridgeServer } from './server';
import { TraceBuffer } from './trace';
import { DEFAULT_WIKI_TIMEOUT_MS, WikiClient } from './wiki';

function parseTimeout(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function isTruthyEnv(value: string | undefined): boolean {
  if (value === undefined) return false;
  const normalized = value.trim().toLowerCase();
  return normalized !== '' && normalized !== '0' && normalized !== 'false' && normalized !== 'no' && normalized !== 'off';
}

function formatError(err: unknown): string {
  if (err instanceof Error) return err.stack || err.message;
  return String(err);
}

async function main(): Promise<void> {
  const projectRoot = path.resolve(process.env.BRIDGE_PROJECT_ROOT ?? process.cwd());
  const wikiUrl = process.env.WIKI_URL?.trim() || '';
  const wikiTimeout = parseTimeout(process.env.WIKI_TIMEOUT_MS, DEFAULT_WIKI_TIMEOUT_MS);
  const traceFlushMs = parseTimeout(process.env.TRACE_FLUSH_MS, 600_000);
  const origin = `${os.userInfo().username}@${os.hostname()}`;
  const wiki = wikiUrl ? new WikiClient(wikiUrl, wikiTimeout) : null;
  const trace =
    wiki && !isTruthyEnv(process.env.BRIDGE_NO_TRACE)
      ? new TraceBuffer(wiki, origin, traceFlushMs)
      : null;

  if (!wiki) {
    console.error('wiki disabled: WIKI_URL is not set');
  }

  const index = await openIndex(projectRoot);
  const server = createBridgeServer(index, wiki, trace ?? undefined);
  const transport = new StdioServerTransport();

  if (trace) {
    trace.start();

    let shuttingDown = false;
    const shutdown = () => {
      if (shuttingDown) return;
      shuttingDown = true;
      // Hard cap in case the final flush hangs; ingestRaw itself times out at 10 s.
      const failsafe = setTimeout(() => process.exit(0), 12_000);
      failsafe.unref();
      trace
        .stop()
        .catch((err) => {
          console.error(`trace shutdown failed: ${formatError(err)}`);
        })
        .finally(() => process.exit(0));
    };

    process.on('SIGINT', shutdown);
    process.on('SIGTERM', shutdown);
    process.stdin.on('close', shutdown);
    process.stdin.on('end', shutdown);
  }

  await server.connect(transport);
}

main().catch((err) => {
  console.error(`bridge startup failed: ${formatError(err)}`);
  process.exit(1);
});
