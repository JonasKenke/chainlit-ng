import {
  ChainlitAPI,
  ChainlitContext,
  type IApproval,
  type IQuestion,
  type IThread,
  getChainlitStore
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SearchChats from '@/components/LeftSidebar/Search';

vi.mock('@/components/i18n', () => ({
  Translator: ({ path }: { path: string }) => <span>{path}</span>,
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const currentThread: IThread = {
  id: 'current-thread',
  name: 'Current conversation',
  createdAt: '2026-01-02T00:00:00Z',
  steps: []
};

const otherThread: IThread = {
  id: 'other-thread',
  name: 'Other conversation',
  createdAt: '2026-01-01T00:00:00Z',
  steps: []
};

const pendingApproval: IApproval = {
  id: 'approval-1',
  stepId: 'step-1',
  threadId: currentThread.id,
  tool: 'send_email',
  status: 'pending',
  live: true
};

const pendingQuestion: IQuestion = {
  id: 'question-1',
  stepId: 'step-1',
  threadId: currentThread.id,
  status: 'pending',
  prompt: 'Continue?',
  options: [],
  allowFreeText: false,
  live: true
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="path">{location.pathname}</output>;
}

function path() {
  return screen.getByTestId('path').textContent;
}

async function setup({
  approvals = [],
  questions = []
}: {
  approvals?: IApproval[];
  questions?: IQuestion[];
} = {}) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const listThreads = vi.spyOn(client, 'listThreads').mockResolvedValue({
    data: [otherThread],
    pageInfo: { hasNextPage: false }
  });
  const store = getChainlitStore(client);
  store.setState({
    currentThreadId: currentThread.id,
    approvals,
    questions
  });

  render(
    <ChainlitContext.Provider value={client}>
      <MemoryRouter initialEntries={[`/thread/${currentThread.id}`]}>
        <SearchChats />
        <LocationProbe />
      </MemoryRouter>
    </ChainlitContext.Provider>
  );

  fireEvent.click(screen.getByRole('button'));
  await screen.findByText('Other conversation', {}, { timeout: 2000 });

  return { listThreads, store };
}

describe('SearchChats pending interaction navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    ['approval', { approvals: [pendingApproval] }],
    ['question', { questions: [pendingQuestion] }]
  ])('blocks navigation with a pending %s', async (_kind, pending) => {
    await setup(pending);

    fireEvent.click(screen.getByText('Other conversation'));

    expect(
      screen.getByText(
        'This chat still has an approval or question waiting. Changing chats will discard it.'
      )
    ).toBeInTheDocument();
    expect(path()).toBe(`/thread/${currentThread.id}`);
  });

  it('stays on the current thread when cancelling the warning', async () => {
    await setup({ approvals: [pendingApproval] });

    fireEvent.click(screen.getByText('Other conversation'));
    fireEvent.click(screen.getByText('Cancel'));

    expect(path()).toBe(`/thread/${currentThread.id}`);
  });

  it('navigates to the target thread after confirming the warning', async () => {
    await setup({ questions: [pendingQuestion] });

    fireEvent.click(screen.getByText('Other conversation'));
    fireEvent.click(screen.getByText('Switch chat'));

    expect(path()).toBe('/thread/other-thread');
  });

  it('navigates normally without a pending interaction', async () => {
    await setup();

    fireEvent.click(screen.getByText('Other conversation'));

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(path()).toBe('/thread/other-thread');
  });
});
