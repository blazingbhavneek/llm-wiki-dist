import { describe, expect, it } from 'vitest';

import { buildFooter, parseFooter, staleness } from '../src/refs';

describe('refs footer', () => {
  it('round-trips a footer with sha and symbols', () => {
    const footer = buildFooter(
      [
        { path: 'graph/store.py', symbol: 'GraphStore' },
        { path: 'bridge/src/server.ts' },
      ],
      'llm-wiki-dist',
      'abcdef1234567890abcdef1234567890abcdef12',
      'alice@host',
    );

    const parsed = parseFooter(`Discovery body${footer}`);
    expect(parsed).toEqual({
      repo: 'llm-wiki-dist',
      sha: 'abcdef1234567890abcdef1234567890abcdef12',
      refs: [
        { path: 'graph/store.py', symbol: 'GraphStore' },
        { path: 'bridge/src/server.ts' },
      ],
    });
  });

  it('parses a footer without sha', () => {
    const parsed = parseFooter(
      [
        'Body text',
        '',
        '---',
        'wiki-refs: llm-wiki-dist',
        '- graph/store.py :: GraphStore',
        'by: alice@host',
      ].join('\n'),
    );

    expect(parsed).toEqual({
      repo: 'llm-wiki-dist',
      sha: null,
      refs: [{ path: 'graph/store.py', symbol: 'GraphStore' }],
    });
  });

  it('returns null when no footer is present', () => {
    expect(parseFooter('plain note body')).toBeNull();
  });

  it('parses ref lines without symbols', () => {
    const parsed = parseFooter(
      [
        'Body text',
        '',
        '---',
        'wiki-refs: llm-wiki-dist @ abcdef1234567890abcdef1234567890abcdef12',
        '- graph/store.py',
        'by: alice@host',
      ].join('\n'),
    );

    expect(parsed).toEqual({
      repo: 'llm-wiki-dist',
      sha: 'abcdef1234567890abcdef1234567890abcdef12',
      refs: [{ path: 'graph/store.py' }],
    });
  });
});

describe('staleness', () => {
  it('flags changed referenced paths using an injected git runner', () => {
    const parsed = {
      repo: 'llm-wiki-dist',
      sha: 'abcdef1234567890abcdef1234567890abcdef12',
      refs: [{ path: 'graph/store.py' }, { path: 'bridge/src/server.ts' }],
    };

    const result = staleness(parsed, '/repo', (args) => {
      if (args[0] === 'remote') return 'https://example.com/org/llm-wiki-dist.git';
      if (args[0] === 'diff') return 'graph/store.py\nfrontend/src/api.js\n';
      throw new Error(`unexpected git args: ${args.join(' ')}`);
    });

    expect(result).toEqual({ stale: true, changed: ['graph/store.py'] });
  });
});
