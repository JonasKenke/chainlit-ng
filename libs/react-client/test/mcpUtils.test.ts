import { describe, expect, it } from 'vitest';

import { IMcp } from '../src/types/mcp';

import {
  applyMcpConnectResponse,
  markMcpAuthorizationRequired,
  markMcpConnected,
  markMcpFailed
} from '../src/utils/mcp';

const pending = (): IMcp[] => [
  {
    name: 'server',
    tools: [],
    status: 'connecting',
    isUserProvided: true,
    clientType: 'streamable-http',
    url: 'https://mcp.example/mcp',
    useOAuth: true
  }
];

describe('markMcpAuthorizationRequired', () => {
  it('keeps the entry connecting and records the authorization URL', () => {
    const mcps = markMcpAuthorizationRequired(
      pending(),
      'server',
      'https://as.example/authorize'
    );

    expect(mcps[0]).toMatchObject({
      status: 'connecting',
      authorizationUrl: 'https://as.example/authorize'
    });
  });

  it('does not resurrect a removed entry', () => {
    expect(
      markMcpAuthorizationRequired([], 'server', 'https://as.example/authorize')
    ).toEqual([]);
  });
});

describe('markMcpConnected', () => {
  it('marks the entry connected and clears the authorization URL', () => {
    const mcps = markMcpConnected(
      [
        {
          ...pending()[0],
          authorizationUrl: 'https://as.example/authorize'
        }
      ],
      { name: 'server', tools: [{ name: 'search' }], status: 'connected' }
    );

    expect(mcps[0]).toMatchObject({
      status: 'connected',
      tools: [{ name: 'search' }]
    });
    expect(mcps[0].authorizationUrl).toBeUndefined();
  });

  it('clears a stale failure detail', () => {
    const mcps = markMcpConnected(
      [
        {
          ...pending()[0],
          status: 'failed',
          detail: 'The MCP authorization failed.'
        }
      ],
      { name: 'server', tools: [], status: 'connected' }
    );

    expect(mcps[0].detail).toBeUndefined();
  });

  it('keeps the list unchanged without an mcp payload', () => {
    const mcps = pending();

    expect(markMcpConnected(mcps, undefined)).toBe(mcps);
  });

  it('does not resurrect a removed entry', () => {
    expect(
      markMcpConnected([], { name: 'server', tools: [], status: 'connected' })
    ).toEqual([]);
  });
});

describe('markMcpFailed', () => {
  it('keeps the backend detail verbatim and leaves the entry removable', () => {
    const detail =
      'Too many MCP authorizations are in progress; finish one first.';
    const mcps = markMcpFailed(pending(), 'server', detail);

    expect(mcps).toHaveLength(1);
    expect(mcps[0]).toMatchObject({ name: 'server', status: 'failed', detail });
    expect(mcps[0].authorizationUrl).toBeUndefined();
  });

  it('does not resurrect a removed entry', () => {
    expect(
      markMcpFailed([], 'server', 'The MCP authorization failed.')
    ).toEqual([]);
  });
});

describe('applyMcpConnectResponse', () => {
  it('treats a 202 as a pending sign-in, not a failure', () => {
    const mcps = applyMcpConnectResponse(pending(), 'server', {
      status: 'authorization_required',
      url: 'https://as.example/authorize',
      mcp: pending()[0]
    });

    expect(mcps[0]).toMatchObject({
      status: 'connecting',
      authorizationUrl: 'https://as.example/authorize'
    });
  });

  it('marks a 200 connected', () => {
    const mcps = applyMcpConnectResponse(pending(), 'server', {
      success: true,
      mcp: { name: 'server', tools: [{ name: 'search' }], status: 'connecting' }
    });

    expect(mcps[0]).toMatchObject({
      status: 'connected',
      tools: [{ name: 'search' }]
    });
  });

  it('does not resurrect a removed entry', () => {
    expect(
      applyMcpConnectResponse([], 'server', {
        status: 'authorization_required',
        url: 'https://as.example/authorize'
      })
    ).toEqual([]);
  });
});
