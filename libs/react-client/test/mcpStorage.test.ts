import { describe, expect, it } from 'vitest';

import { parseStoredMcps } from '../src/mcpStorage';

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
      { name: 'files', status: 'connected', tools, type: 'stdio' }
    ]);
  });

  it('migrates legacy URL entries to user-provided servers', () => {
    const headers = { Authorization: 'Bearer token' };
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
        url: 'https://http.example/mcp',
        headers,
        tools
      }
    ]);

    expect(parseStoredMcps(stored)).toEqual([
      {
        name: 'sse',
        status: 'failed',
        tools,
        clientType: 'sse',
        isUserProvided: true,
        url: 'https://sse.example/mcp'
      },
      {
        name: 'http',
        status: 'connected',
        tools,
        clientType: 'streamable-http',
        isUserProvided: true,
        url: 'https://http.example/mcp',
        headers
      }
    ]);
  });

  it('keeps entries already written in the new shape', () => {
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

    expect(parseStoredMcps(JSON.stringify(entries))).toEqual(entries);
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
});
