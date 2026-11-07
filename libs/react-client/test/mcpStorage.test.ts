import { describe, expect, it } from 'vitest';

import {
  parseStoredMcps,
  sanitizeMcpUrl,
  serializeStoredMcps
} from '../src/mcpStorage';

const tools = [{ name: 'search' }];

describe('parseStoredMcps', () => {
  it('returns an empty list for corrupt JSON without throwing', () => {
    expect(() => parseStoredMcps('{"name":')).not.toThrow();
    expect(parseStoredMcps('{"name":')).toEqual([]);
  });

  it('returns an empty list for non-array payloads', () => {
    expect(parseStoredMcps(null)).toEqual([]);
    expect(parseStoredMcps(JSON.stringify({ name: 'server' }))).toEqual([]);
  });

  it('migrates a legacy stdio entry and drops its command', () => {
    const stored = JSON.stringify([
      {
        name: 'files',
        status: 'connected',
        clientType: 'stdio',
        command: 'npx -y files-mcp',
        tools
      }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      { name: 'files', status: 'connected', tools: [], type: 'stdio' }
    ]);
  });

  it('migrates legacy URL entries and drops stored credentials', () => {
    const stored = JSON.stringify([
      {
        name: 'sse',
        status: 'failed',
        clientType: 'sse',
        url: 'https://sse.example/mcp',
        tools
      },
      {
        name: 'http',
        status: 'connected',
        clientType: 'streamable-http',
        url: 'https://user:pass@http.example/mcp?token=secret#frag',
        headers: { Authorization: 'Bearer token' },
        tools
      }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      {
        name: 'sse',
        status: 'failed',
        tools: [],
        clientType: 'sse',
        isUserProvided: true,
        url: 'https://sse.example/mcp'
      },
      {
        name: 'http',
        status: 'connected',
        tools: [],
        clientType: 'streamable-http',
        isUserProvided: true,
        url: 'https://http.example/mcp'
      }
    ]);
  });

  it('keeps entries already written in the new shape without persisted tools', () => {
    const entries = [
      { name: 'named', status: 'connected', type: 'streamable-http', tools },
      {
        name: 'user',
        status: 'failed',
        clientType: 'sse',
        isUserProvided: true,
        url: 'https://user.example/mcp',
        tools
      }
    ];

    expect(parseStoredMcps(JSON.stringify(entries))).toEqual([
      {
        name: 'named',
        status: 'connected',
        type: 'streamable-http',
        tools: []
      },
      {
        name: 'user',
        status: 'failed',
        clientType: 'sse',
        isUserProvided: true,
        url: 'https://user.example/mcp',
        tools: []
      }
    ]);
  });

  it('drops malformed entries and keeps valid ones', () => {
    const stored = JSON.stringify([
      null,
      42,
      'not-a-server',
      { status: 'connected', clientType: 'sse', url: 'https://missing.name' },
      { name: 'no-transport', status: 'connected' },
      { name: 'unknown-transport', clientType: 'websocket', url: 'https://x' },
      { name: 'missing-url', clientType: 'sse' },
      { name: 'named', status: 'connected', type: 'stdio' }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      { name: 'named', status: 'connected', type: 'stdio', tools: [] }
    ]);
  });

  it('defaults a missing or unknown status to failed', () => {
    const stored = JSON.stringify([
      { name: 'missing', type: 'sse' },
      { name: 'unknown', type: 'sse', status: 'bogus' }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      { name: 'missing', type: 'sse', status: 'failed', tools: [] },
      { name: 'unknown', type: 'sse', status: 'failed', tools: [] }
    ]);
  });

  it('keeps only the first entry for a duplicated name', () => {
    const stored = JSON.stringify([
      { name: 'dup', status: 'connected', type: 'sse' },
      { name: 'dup', status: 'failed', type: 'stdio' }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      { name: 'dup', status: 'connected', type: 'sse', tools: [] }
    ]);
  });
});

describe('serializeStoredMcps', () => {
  it('never persists headers, tools or URL credentials', () => {
    const stored = serializeStoredMcps([
      {
        name: 'user',
        status: 'connected',
        tools,
        isUserProvided: true,
        clientType: 'sse',
        url: 'https://user:pass@example.com/mcp?token=query-secret#frag',
        headers: { Authorization: 'Bearer header-secret' }
      }
    ]);

    expect(stored).not.toContain('header-secret');
    expect(stored).not.toContain('query-secret');
    expect(stored).not.toContain('pass@');
    expect(stored).not.toContain('tools');
    expect(JSON.parse(stored)).toEqual([
      {
        name: 'user',
        status: 'connected',
        isUserProvided: true,
        clientType: 'sse',
        url: 'https://example.com/mcp'
      }
    ]);
  });

  it('drops user servers whose URL has no safe form', () => {
    const stored = serializeStoredMcps([
      {
        name: 'bad',
        status: 'failed',
        tools: [],
        isUserProvided: true,
        clientType: 'sse',
        url: 'not a url'
      }
    ]);

    expect(stored).toBe('[]');
  });
});

describe('sanitizeMcpUrl', () => {
  it('strips userinfo, query and fragment', () => {
    expect(
      sanitizeMcpUrl('https://user:pass@example.com/mcp?token=secret#frag')
    ).toBe('https://example.com/mcp');
  });

  it('returns undefined for invalid URLs', () => {
    expect(sanitizeMcpUrl('not a url')).toBeUndefined();
    expect(sanitizeMcpUrl(undefined)).toBeUndefined();
  });
});
