import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ChainlitAPI,
  ChainlitContext,
  getChainlitStore,
  useChatSession
} from '@chainlit/react-client';

import { io, sockets } from './mocks/socket.io-client';

function deferredStickyCookie(client: ChainlitAPI) {
  const pending = new Map<string, () => void>();
  vi.spyOn(client, 'stickyCookie').mockImplementation(
    (sessionId) =>
      new Promise((resolve) => {
        pending.set(sessionId, () => resolve({}));
      })
  );
  return pending;
}

function renderSession(client: ChainlitAPI) {
  let session!: ReturnType<typeof useChatSession>;

  function Harness() {
    session = useChatSession();
    return null;
  }

  render(
    <ChainlitContext.Provider value={client}>
      <Harness />
    </ChainlitContext.Provider>
  );

  return session;
}

describe('useChatSession connect ordering', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  it('keeps the newer thread connect when an older connect resolves last', async () => {
    vi.useFakeTimers();
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    const store = getChainlitStore(client);
    store.setState({ threadIdToResume: undefined, sessionId: 'session-a' });
    const pending = deferredStickyCookie(client);
    const session = renderSession(client);

    // Transient "/" connect starts and waits on its sticky cookie.
    act(() => {
      session.connect({ userEnv: {} });
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(pending.has('session-a')).toBe(true);

    // The restore connect starts after the thread id is known.
    act(() => {
      store.setState({ threadIdToResume: 'test1', sessionId: 'session-b' });
    });
    act(() => {
      session.connect({ userEnv: {} });
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(pending.has('session-b')).toBe(true);

    // The restore connect wins the cookie race and opens the resume socket.
    await act(async () => {
      pending.get('session-b')!();
    });
    expect(store.getState().session?.socket.auth.threadId).toBe('test1');

    // The stale connect must not replace the resume socket when it finishes.
    await act(async () => {
      pending.get('session-a')!();
    });
    expect(store.getState().session?.socket.auth.threadId).toBe('test1');
    expect(io).toHaveBeenCalledTimes(1);
    expect(sockets).toHaveLength(1);
  });
});
