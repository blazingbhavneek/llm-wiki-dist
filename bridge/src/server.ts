import os from 'os';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

import type { CodeIndex } from './codegraph';
import { buildFooter, clampText, headSha, parseFooter, repoName, staleness, type Ref } from './refs';
import type { TraceBuffer } from './trace';
import type { WikiClient, WikiNote } from './wiki';

const TEAM_KNOWLEDGE_HEADER = '── チームの知見（Wikiより） ──';
const MAX_NOTE_BLOCK_CHARS = 600;
const MAX_WIKI_SECTION_CHARS = 2000;

const SAVE_NOTE_DESCRIPTION =
  '恒久的で再利用可能な発見をチームWikiに保存します：根本原因、不変条件、落とし穴、意思決定。セッションの経過報告、自明な事実、コードを数秒読めば分かることは保存しないでください。コードに関する発見の場合は、関係するファイル/シンボルを refs に列挙してください。';

// Vendored codegraph ships an English tool description; the wiki product is
// Japanese-facing, so override what the agent LLM sees without forking vendor/.
const TOOL_DESCRIPTION_OVERRIDES: Record<string, string> = {
  codegraph_explore:
    '主要ツール — ほぼすべての質問、または編集の前に最初に呼び出してください：Xはどう動くか、アーキテクチャ、バグ、Xはどこ/何か、ある領域の概観、これから変更するシンボルの確認など。関連シンボルの逐語的なソースコードをファイル別にまとめて1回の呼び出しで返します（Read と同等 — 表示されたソースは既に読んだものとして扱い、同じファイルを再度開かないでください）。加えてシンボル間の呼び出し経路も返します。クエリは自然言語の質問でも、シンボル名/ファイル名の羅列でもかまいません。通常はこの1回の呼び出しだけで十分です — search/Read/Grep のループより少ないトークンと往復で、より正確なコンテキストが得られます。',
};

const wikiAskInputSchema = {
  type: 'object',
  properties: {
    question: { type: 'string' },
  },
  required: ['question'],
} as const;

const saveNoteInputSchema = {
  type: 'object',
  properties: {
    title: { type: 'string', description: '発見の短いタイトル' },
    body: { type: 'string', description: '発見の内容（Markdown）' },
    refs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          symbol: { type: 'string' },
        },
        required: ['path'],
      },
    },
  },
  required: ['title', 'body', 'refs'],
} as const;

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function normalizeString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function normalizeRefs(value: unknown): Ref[] {
  if (!Array.isArray(value)) return [];
  const refs: Ref[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const pathValue = normalizeString((item as { path?: unknown }).path).trim();
    if (!pathValue) continue;
    const symbolValue = normalizeString((item as { symbol?: unknown }).symbol).trim();
    const ref: Ref = { path: pathValue };
    if (symbolValue) ref.symbol = symbolValue;
    refs.push(ref);
  }
  return refs;
}

function formatNoteBlock(note: WikiNote, projectRoot: string): string {
  const title = clampText(normalizeString(note.title).trim() || '（無題）', 120);
  const summarySource =
    normalizeString(note.summary).trim() || normalizeString(note.body).slice(0, 400).trim();
  const lines = [
    `### ${title}`,
    clampText(summarySource || '（要約なし）', 400),
    `（保存日時 ${note.created_at}）`,
  ];

  const parsed = parseFooter(note.body);
  if (parsed) {
    const status = staleness(parsed, projectRoot);
    if (status) {
      const sha7 = parsed.sha ? parsed.sha.slice(0, 7) : '';
      lines.push(
        status.stale
          ? `⚠️ 古い可能性あり：${sha7} 以降に変更されたファイル: ${status.changed.join(', ')} — 信頼する前に確認してください`
          : `✅ ${sha7} 以降、参照ファイルに変更なし`,
      );
    }
  }

  return clampText(lines.join('\n'), MAX_NOTE_BLOCK_CHARS);
}

function formatKnowledgeSection(notes: WikiNote[], projectRoot: string): string {
  let section = TEAM_KNOWLEDGE_HEADER;

  for (const note of notes) {
    const block = formatNoteBlock(note, projectRoot);
    const candidate = `${section}\n\n${block}`;
    if (candidate.length <= MAX_WIKI_SECTION_CHARS) {
      section = candidate;
      continue;
    }

    const remaining = MAX_WIKI_SECTION_CHARS - section.length - 2;
    if (remaining > 0) {
      section = `${section}\n\n${clampText(block, remaining)}`;
    }
    break;
  }

  return section === TEAM_KNOWLEDGE_HEADER ? '' : section;
}

async function appendWikiKnowledge(
  baseText: string,
  query: string,
  wiki: WikiClient | null,
  projectRoot: string,
): Promise<string> {
  if (!wiki) return baseText;

  try {
    const notes = await wiki.search(query, 3);
    const section = formatKnowledgeSection(notes, projectRoot);
    if (!section) return baseText;
    return `${baseText}\n\n${section}`;
  } catch (err) {
    console.error(`wiki piggyback skipped: ${formatError(err)}`);
    return baseText;
  }
}

function textResult(text: string, isError = false) {
  return {
    content: [{ type: 'text' as const, text }],
    isError,
  };
}

export function createBridgeServer(
  index: CodeIndex,
  wiki: WikiClient | null,
  trace?: TraceBuffer,
): Server {
  const codegraphToolNames = new Set(index.tools.map((tool) => tool.name));
  const wikiTools = wiki
    ? [
        {
          name: 'wiki_ask',
          description:
            'チームの知識Wikiに質問します。検索より低速です（サーバー側でリサーチエージェントが動きます）。ローカルのコードコンテキストだけでは足りない場合に使ってください。',
          inputSchema: wikiAskInputSchema,
        },
        {
          name: 'save_note',
          description: SAVE_NOTE_DESCRIPTION,
          inputSchema: saveNoteInputSchema,
        },
      ]
    : [];

  const tools = [
    ...index.tools.map((tool) => ({
      name: tool.name,
      description: TOOL_DESCRIPTION_OVERRIDES[tool.name] ?? tool.description,
      inputSchema: tool.inputSchema,
    })),
    ...wikiTools,
  ];

  const server = new Server(
    { name: 'team-memory-bridge', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const name = request.params.name;
    const args = (request.params.arguments ?? {}) as Record<string, unknown>;

    const respond = (text: string, isError = false) => {
      try {
        trace?.record(name, args, text);
      } catch (err) {
        console.error(`trace record skipped: ${formatError(err)}`);
      }
      return textResult(text, isError);
    };

    if (codegraphToolNames.has(name)) {
      try {
        const result = await index.execute(name, args);
        if (result.isError) {
          return respond(result.text, true);
        }

        let text = result.text;
        if (name === 'codegraph_explore') {
          const query = normalizeString(args.query).trim();
          if (query) {
            text = await appendWikiKnowledge(text, query, wiki, index.projectRoot);
          }
        }
        return respond(text, false);
      } catch (err) {
        console.error(`codegraph tool failed: ${formatError(err)}`);
        return respond(formatError(err), true);
      }
    }

    if (name === 'wiki_ask') {
      if (!wiki) {
        return respond('WIKI_URL が未設定のため、wiki ツールは無効です', true);
      }

      try {
        const question = normalizeString(args.question).trim();
        if (!question) {
          return respond('question は必須です', true);
        }
        const answer = await wiki.ask(question);
        const text = await appendWikiKnowledge(answer, question, wiki, index.projectRoot);
        return respond(text, false);
      } catch (err) {
        console.error(`wiki_ask failed: ${formatError(err)}`);
        return respond(formatError(err), true);
      }
    }

    if (name === 'save_note') {
      if (!wiki) {
        return respond('WIKI_URL が未設定のため、wiki ツールは無効です', true);
      }

      try {
        const title = normalizeString(args.title).trim();
        const body = normalizeString(args.body);
        const refs = normalizeRefs(args.refs);
        const origin = `${os.userInfo().username}@${os.hostname()}`;
        const footer = buildFooter(
          refs,
          repoName(index.projectRoot),
          headSha(index.projectRoot),
          origin,
        );
        const savedId = await wiki.saveNote(`${body}${footer}`, origin, title || null);
        return respond(`ノード ${savedId} として保存しました`, false);
      } catch (err) {
        console.error(`save_note failed: ${formatError(err)}`);
        return respond(formatError(err), true);
      }
    }

    return respond(`不明なツール: ${name}`, true);
  });

  return server;
}
