// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useSessionManagement } from '../src/api/hooks/auth/sessionManagement';

import { IMcp } from '../src/types/mcp';

import { ChainlitContext } from '../src/context';
import { MCP_STORAGE_KEY } from '../src/mcpStorage';
import { useChainlitState } from '../src/state';

const createWrapper = (client: any) => {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(ChainlitContext.Provider, { value: client }, children);
};

describe('MCP credential storage', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('keeps headers in memory but never persists them', () => {
    const client = { logout: vi.fn() };
    const { result } = renderHook(() => useChainlitState('mcps'), {
      wrapper: createWrapper(client)
    });
    const raw = 'https://user:pass@example.com/mcp?token=query-secret#frag';
    const mcp: IMcp = {
      name: 'user',
      status: 'connected',
      tools: [{ name: 'tool' }],
      isUserProvided: true,
      clientType: 'sse',
      url: raw,
      headers: { Authorization: 'Bearer header-secret' }
    };

    act(() => {
      result.current[1]([mcp]);
    });

    const stored = localStorage.getItem(MCP_STORAGE_KEY) || '';
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
    // The reconnect payload stays available for the current session.
    expect(result.current[0][0].headers).toEqual({
      Authorization: 'Bearer header-secret'
    });
    expect(result.current[0][0].url).toBe(raw);
  });

  it('clears persisted and in-memory MCPs on logout', async () => {
    localStorage.setItem(
      MCP_STORAGE_KEY,
      JSON.stringify([{ name: 'stale', status: 'connected', type: 'sse' }])
    );
    const client = { logout: vi.fn().mockResolvedValue(undefined) };
    const { result } = renderHook(
      () => ({
        session: useSessionManagement(),
        mcps: useChainlitState('mcps')
      }),
      { wrapper: createWrapper(client) }
    );

    expect(result.current.mcps[0]).toHaveLength(1);

    await act(async () => {
      await result.current.session.logout();
    });

    expect(client.logout).toHaveBeenCalledOnce();
    expect(localStorage.getItem(MCP_STORAGE_KEY)).toBeNull();
    expect(result.current.mcps[0]).toEqual([]);
  });

  it('purges legacy credentials from the raw key on store init', () => {
    localStorage.setItem(
      MCP_STORAGE_KEY,
      JSON.stringify([
        {
          name: 'legacy',
          status: 'connected',
          clientType: 'sse',
          url: 'https://user:pass@example.com/mcp?token=query-secret',
          headers: { Authorization: 'Bearer header-secret' },
          tools: [{ name: 'search' }]
        }
      ])
    );
    const client = { logout: vi.fn() };

    const { result } = renderHook(() => useChainlitState('mcps'), {
      wrapper: createWrapper(client)
    });

    const stored = localStorage.getItem(MCP_STORAGE_KEY) || '';
    expect(stored).not.toContain('header-secret');
    expect(stored).not.toContain('query-secret');
    expect(stored).not.toContain('pass@');
    expect(JSON.parse(stored)).toEqual([
      {
        name: 'legacy',
        status: 'connected',
        isUserProvided: true,
        clientType: 'sse',
        url: 'https://example.com/mcp'
      }
    ]);
    expect(result.current[0][0].url).toBe('https://example.com/mcp');
  });

  it('removes a corrupt raw key on store init', () => {
    localStorage.setItem(MCP_STORAGE_KEY, '{"name":');
    const client = { logout: vi.fn() };

    renderHook(() => useChainlitState('mcps'), {
      wrapper: createWrapper(client)
    });

    expect(localStorage.getItem(MCP_STORAGE_KEY)).toBeNull();
  });
});
