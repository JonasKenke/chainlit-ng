import {
  ChainlitAPI,
  ChainlitContext,
  getChainlitStore,
  useChatInteract,
  useChatSession
} from '@chainlit-ng/react-client';
import type { IMessageElement } from '@chainlit-ng/react-client';
import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

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

  it('marks a socket approval live and keeps its edit fields', async () => {
    const { store } = await connectWithResumeHandler();
    const socket = sockets[0];
    const handler = socket.on.mock.calls.find(
      ([event]: [string]) => event === 'approval'
    )?.[1] as (approval: unknown) => void;
    expect(handler).toBeTruthy();

    act(() => {
      handler({
        id: 'approval-1',
        stepId: 'step-1',
        threadId: 'thread-1',
        tool: 'send_email',
        status: 'pending',
        revision: 1,
        arguments: { amount: 1 },
        editAllowed: true,
        editFields: [
          {
            name: 'amount',
            label: 'Amount',
            kind: 'integer',
            required: true,
            nullable: false,
            secret: false
          }
        ]
      });
    });

    const [approval] = store.getState().approvals;
    expect(approval.live).toBe(true);
    expect(approval.editFields).toHaveLength(1);
  });
});

describe('useChatSession question events', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  it('upserts a live question and ignores a late pending event', async () => {
    const { store } = await connectWithResumeHandler();
    const socket = sockets[0];
    const handler = socket.on.mock.calls.find(
      ([event]: [string]) => event === 'question'
    )?.[1] as (question: unknown) => void;
    expect(handler).toBeTruthy();

    const base = {
      kind: 'question',
      id: 'question-1',
      stepId: 'step-1',
      threadId: 'thread-1',
      prompt: 'Which environment?',
      options: [{ id: 'staging', label: 'Staging' }],
      allowFreeText: true
    };
    act(() => {
      handler({ ...base, status: 'pending', revision: 1 });
    });
    expect(store.getState().questions).toHaveLength(1);
    expect(store.getState().questions[0].live).toBe(true);

    act(() => {
      handler({
        ...base,
        status: 'answered',
        answer: { optionId: 'staging', label: 'Staging' },
        revision: 2
      });
    });
    expect(store.getState().questions[0].status).toBe('answered');

    act(() => {
      handler({ ...base, status: 'pending', revision: 1 });
    });
    expect(store.getState().questions[0].status).toBe('answered');
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

describe('useChatSession question resume', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  const record = {
    kind: 'question',
    id: 'question-1',
    stepId: 'step-1',
    threadId: 'thread-1',
    status: 'answered',
    prompt: 'What should I name the release?',
    options: [],
    allowFreeText: true,
    answer: { text: 'Kestrel' },
    decidedAt: '2026-11-09T00:00:00Z',
    terminalAt: '2026-11-09T00:00:00Z'
  };

  it('hydrates an answered question from the resumed thread as read-only', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'question-step',
            type: 'tool',
            metadata: { approval: record }
          }
        ],
        elements: []
      });
    });

    const [question] = store.getState().questions;
    expect(question.id).toBe('question-1');
    expect(question.live).toBe(false);
    expect(question.status).toBe('answered');
    expect(question.answer).toEqual({ text: 'Kestrel' });
  });

  it('keeps a live card answerable when the same session reconnects', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();
    act(() => {
      store.setState({
        questions: [
          {
            ...record,
            status: 'pending',
            answer: null,
            decidedAt: null,
            terminalAt: null,
            live: true
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
            name: 'question-step',
            type: 'tool',
            metadata: {
              approval: {
                ...record,
                status: 'pending',
                answer: null,
                decidedAt: null,
                terminalAt: null
              }
            }
          }
        ],
        elements: []
      });
    });

    const questions = store.getState().questions;
    expect(questions).toHaveLength(1);
    expect(questions[0].status).toBe('pending');
    expect(questions[0].live).toBe(true);
  });
});

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
                reason: 'not now',
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
    expect(approval.live).toBe(false);
    expect(approval.reason).toBe('not now');
    expect(approval.arguments).toEqual({ to: 'person@example.com' });
  });

  it('hydrates the persisted markdown content from the resumed thread', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'apply_changes',
            type: 'tool',
            metadata: {
              approval: {
                id: 'approval-1',
                stepId: 'step-1',
                threadId: 'thread-1',
                tool: 'apply_changes',
                status: 'orphaned',
                revision: 2,
                content: '| change | risk |\n| --- | --- |\n| deploy | low |',
                argumentsPreview: { target: 'production' }
              }
            }
          }
        ],
        elements: []
      });
    });

    const [approval] = store.getState().approvals;
    expect(approval.content).toContain('| deploy | low |');
    expect(approval.title).toBeNull();
  });

  it('does not hydrate a question record as an approval', async () => {
    const { store, resumeHandler } = await connectWithResumeHandler();

    act(() => {
      resumeHandler({
        id: 'thread-1',
        steps: [
          {
            id: 'step-1',
            name: 'question-step',
            type: 'tool',
            metadata: {
              approval: {
                kind: 'question',
                id: 'question-1',
                stepId: 'step-1',
                threadId: 'thread-1',
                status: 'answered'
              }
            }
          }
        ],
        elements: []
      });
    });

    expect(store.getState().approvals).toEqual([]);
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

describe('useChatSession thread title events', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    io.mockClear();
    sockets.length = 0;
  });

  async function threadTitleHandler() {
    const { store } = await connectWithResumeHandler();
    const socket = sockets[0];
    const handler = socket.on.mock.calls.find(
      ([event]: [string]) => event === 'thread_title_updated'
    )?.[1] as (event: { thread_id: string; name: string }) => void;
    expect(handler).toBeTruthy();
    return { store, handler };
  }

  it('renames a thread and recomputes grouped threads in place', async () => {
    const { store, handler } = await threadTitleHandler();
    const createdAt = new Date().toISOString();
    act(() => {
      store.setState({
        threadHistory: {
          threads: [
            { id: 'thread-1', name: 'Old title', createdAt, steps: [] },
            { id: 'thread-2', name: 'Other title', createdAt, steps: [] }
          ],
          currentThreadId: 'thread-1',
          pageInfo: { hasNextPage: false }
        }
      });
    });

    act(() => {
      handler({ thread_id: 'thread-1', name: 'New title' });
    });

    const history = store.getState().threadHistory;
    expect(
      history?.threads?.find((thread) => thread.id === 'thread-1')?.name
    ).toBe('New title');
    expect(
      history?.threads?.find((thread) => thread.id === 'thread-2')?.name
    ).toBe('Other title');
    expect(history?.timeGroupedThreads).toBeDefined();
    expect(
      Object.values(history!.timeGroupedThreads!)
        .flat()
        .find((thread) => thread.id === 'thread-1')?.name
    ).toBe('New title');
  });

  it('ignores unknown thread ids without disturbing history', async () => {
    const { store, handler } = await threadTitleHandler();
    const createdAt = new Date().toISOString();
    act(() => {
      store.setState({
        threadHistory: {
          threads: [
            { id: 'thread-1', name: 'Old title', createdAt, steps: [] }
          ],
          currentThreadId: 'thread-1'
        }
      });
    });

    act(() => {
      handler({ thread_id: 'missing-thread', name: 'New title' });
    });

    const history = store.getState().threadHistory;
    expect(history?.threads).toHaveLength(1);
    expect(history?.threads?.[0].name).toBe('Old title');
  });

  it('leaves history undefined when threads are not loaded', async () => {
    const { store, handler } = await threadTitleHandler();
    act(() => {
      store.setState({ threadHistory: undefined });
    });

    act(() => {
      handler({ thread_id: 'thread-1', name: 'New title' });
    });

    expect(store.getState().threadHistory).toBeUndefined();
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
