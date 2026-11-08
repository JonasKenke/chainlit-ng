import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ChainlitAPI,
  ChainlitContext,
  getChainlitStore,
  useChatInteract,
  useChatSession
} from '@chainlit/react-client';
import type { IMessageElement } from '@chainlit/react-client';

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

describe('useChatSession approval events', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  it('ignores a late pending event after a terminal decision', async () => {
    vi.useFakeTimers();
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    const store = getChainlitStore(client);
    store.setState({ threadIdToResume: undefined, sessionId: 'session-a' });
    const pending = deferredStickyCookie(client);
    const session = renderSession(client);

    act(() => {
      session.connect({ userEnv: {} });
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    await act(async () => {
      pending.get('session-a')!();
    });

    const socket = sockets[0];
    const handler = socket.on.mock.calls.find(
      ([event]: [string]) => event === 'approval'
    )?.[1] as (approval: unknown) => void;
    expect(handler).toBeTruthy();

    const base = {
      id: 'approval-1',
      stepId: 'step-1',
      threadId: 'thread-1',
      tool: 'send_email',
      requestedBy: 'alice',
      arguments: { amount: 1 }
    };
    act(() => {
      store.setState({
        approvals: [
          {
            ...base,
            status: 'approved',
            decision: 'approve',
            revision: 2
          }
        ]
      });
    });
    act(() => {
      handler({ ...base, status: 'pending', revision: 1 });
    });

    expect(store.getState().approvals[0].status).toBe('approved');
  });
});

async function connectWithResumeHandler() {
  vi.useFakeTimers();
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const store = getChainlitStore(client);
  store.setState({ threadIdToResume: undefined, sessionId: 'session-a' });
  const pending = deferredStickyCookie(client);
  const session = renderSession(client);

  act(() => {
    session.connect({ userEnv: {} });
  });
  await act(() => vi.advanceTimersByTimeAsync(200));
  await act(async () => {
    pending.get('session-a')!();
  });

  const socket = sockets[0];
  const resumeHandler = socket.on.mock.calls.find(
    ([event]: [string]) => event === 'resume_thread'
  )?.[1] as (thread: unknown) => void;

  return { store, resumeHandler };
}

describe('useChatSession approval resume', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  it('hydrates a terminal approval from the resumed thread as read-only', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'send_email',
            type: 'tool',
            metadata: {
              approval: {
                id: 'approval-1',
                stepId: 'step-1',
                threadId: 'thread-1',
                tool: 'send_email',
                status: 'orphaned',
                revision: 2,
                terminalReason: 'server_restart',
                argumentsPreview: { to: 'person@example.com' }
              }
            }
          }
        ],
        elements: []
      });
    });

    const [approval] = store.getState().approvals;
    expect(approval.id).toBe('approval-1');
    expect(approval.status).toBe('orphaned');
    expect(approval.arguments).toEqual({ to: 'person@example.com' });
  });

  it('keeps the live arguments when a pending record is restored', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();
    act(() => {
      store.setState({
        approvals: [
          {
            id: 'approval-1',
            stepId: 'step-1',
            threadId: 'thread-1',
            tool: 'send_email',
            status: 'pending',
            revision: 1,
            arguments: {
              apiKey: 'server-held-secret',
              to: 'person@example.com'
            }
          }
        ]
      });
    });

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'send_email',
            type: 'tool',
            metadata: {
              approval: {
                id: 'approval-1',
                stepId: 'step-1',
                threadId: 'thread-1',
                tool: 'send_email',
                status: 'pending',
                revision: 1,
                argumentsPreview: {
                  apiKey: '[redacted]',
                  to: 'person@example.com'
                }
              }
            }
          }
        ],
        elements: []
      });
    });

    const approvals = store.getState().approvals;
    expect(approvals).toHaveLength(1);
    expect(approvals[0].status).toBe('pending');
    expect(approvals[0].arguments).toEqual({
      apiKey: 'server-held-secret',
      to: 'person@example.com'
    });
  });

  it('applies a terminal record to an existing live card', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();
    act(() => {
      store.setState({
        approvals: [
          {
            id: 'approval-1',
            stepId: 'step-1',
            threadId: 'thread-1',
            tool: 'send_email',
            status: 'pending',
            revision: 1,
            arguments: { apiKey: 'server-held-secret' }
          }
        ]
      });
    });

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'send_email',
            type: 'tool',
            metadata: {
              approval: {
                id: 'approval-1',
                stepId: 'step-1',
                threadId: 'thread-1',
                tool: 'send_email',
                status: 'superseded',
                revision: 2,
                terminalReason: 'stopped',
                argumentsPreview: { apiKey: '[redacted]' }
              }
            }
          }
        ],
        elements: []
      });
    });

    const [approval] = store.getState().approvals;
    expect(approval.status).toBe('superseded');
    expect(approval.terminalReason).toBe('stopped');
  });
});

describe('useChatInteract clear', () => {
  it('drops approvals from the previous chat', () => {
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    const store = getChainlitStore(client);
    store.setState({
      sessionId: 'session-a',
      approvals: [
        {
          id: 'approval-1',
          stepId: 'step-1',
          threadId: 'thread-1',
          tool: 'send_email',
          status: 'pending',
          arguments: { amount: 1 }
        }
      ]
    });

    let interact!: ReturnType<typeof useChatInteract>;
    function Harness() {
      interact = useChatInteract();
      return null;
    }
    render(
      <ChainlitContext.Provider value={client}>
        <Harness />
      </ChainlitContext.Provider>
    );

    act(() => interact.clear());

    expect(store.getState().approvals).toEqual([]);
  });
});

describe('useChatSession open_side_view', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  const element: IMessageElement = {
    id: 'element-1',
    type: 'text',
    name: 'Details',
    display: 'side',
    forId: 'step-1'
  };

  async function openSideViewHandler() {
    const { store } = await connectWithResumeHandler();
    const socket = sockets[0];
    const handler = socket.on.mock.calls.find(
      ([event]: [string]) => event === 'open_side_view'
    )?.[1] as (payload: { id: string }) => void;
    expect(handler).toBeTruthy();
    return { store, handler };
  }

  it('opens the side view for a delivered element', async () => {
    const { store, handler } = await openSideViewHandler();

    act(() => {
      store.setState({ elements: [element] });
      handler({ id: element.id });
    });

    expect(store.getState().sideView).toEqual({
      title: element.name,
      elements: [element]
    });
  });

  it('leaves the side view unchanged for an unknown id', async () => {
    const { store, handler } = await openSideViewHandler();

    act(() => {
      store.setState({ elements: [element], sideView: undefined });
      handler({ id: 'missing-element' });
    });

    expect(store.getState().sideView).toBeUndefined();
  });
});
