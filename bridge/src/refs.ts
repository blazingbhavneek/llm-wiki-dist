import { execFileSync } from 'child_process';
import path from 'path';

export interface Ref {
  path: string;
  symbol?: string;
}

export interface ParsedRefs {
  repo: string;
  sha: string | null;
  refs: Ref[];
}

export type GitRunner = (args: string[]) => string;

function makeGitRunner(root: string): GitRunner {
  return (args: string[]) =>
    execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  if (max <= 1) return text.slice(0, max);
  return `${text.slice(0, max - 1)}…`;
}

export function repoName(root: string, runGit: GitRunner = makeGitRunner(root)): string {
  try {
    const remote = runGit(['remote', 'get-url', 'origin']).trim();
    if (!remote) return path.basename(root);
    const base = remote.replace(/\/+$/, '').split('/').pop() ?? remote;
    return base.replace(/\.git$/i, '') || path.basename(root);
  } catch {
    return path.basename(root);
  }
}

export function headSha(root: string, runGit: GitRunner = makeGitRunner(root)): string | null {
  try {
    const sha = runGit(['rev-parse', 'HEAD']).trim();
    return sha || null;
  } catch {
    return null;
  }
}

export function buildFooter(
  refs: Ref[],
  repo: string,
  sha: string | null,
  origin: string,
): string {
  const lines = ['', '', '---', sha ? `wiki-refs: ${repo} @ ${sha}` : `wiki-refs: ${repo}`];

  for (const ref of refs) {
    if (ref.symbol) {
      lines.push(`- ${ref.path} :: ${ref.symbol}`);
    } else {
      lines.push(`- ${ref.path}`);
    }
  }

  lines.push(`by: ${origin}`);
  return lines.join('\n');
}

export function parseFooter(body: string): ParsedRefs | null {
  const lines = body.split(/\r?\n/);
  const headerRe = /^wiki-refs:\s*(\S+)(?:\s*@\s*([0-9a-f]{7,40}))?\s*$/;
  const refRe = /^-\s+(\S+)(?:\s*::\s*(\S+))?\s*$/;

  for (let i = lines.length - 1; i >= 1; i -= 1) {
    if (lines[i - 1].trim() !== '---') continue;
    const headerMatch = lines[i].match(headerRe);
    if (!headerMatch) continue;

    const refs: Ref[] = [];
    for (let j = i + 1; j < lines.length; j += 1) {
      const refMatch = lines[j].match(refRe);
      if (!refMatch) break;
      const ref: Ref = { path: refMatch[1] };
      if (refMatch[2]) ref.symbol = refMatch[2];
      refs.push(ref);
    }

    return {
      repo: headerMatch[1],
      sha: headerMatch[2] ?? null,
      refs,
    };
  }

  return null;
}

export function staleness(
  parsed: ParsedRefs,
  projectRoot: string,
  runGit: GitRunner = makeGitRunner(projectRoot),
): { stale: boolean; changed: string[] } | null {
  if (!parsed.sha) return null;
  if (parsed.repo !== repoName(projectRoot, runGit)) return null;

  try {
    const raw = runGit(['diff', '--name-only', `${parsed.sha}..HEAD`]).trim();
    if (!raw) return { stale: false, changed: [] };

    const refPaths = new Set(parsed.refs.map((ref) => ref.path));
    const changed = raw
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line) => refPaths.has(line));

    return {
      stale: changed.length > 0,
      changed,
    };
  } catch {
    return null;
  }
}

export function clampText(text: string, max: number): string {
  return truncate(text, max);
}
