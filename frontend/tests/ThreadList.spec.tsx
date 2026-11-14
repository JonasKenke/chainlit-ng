import {
  ChainlitAPI,
  ChainlitContext,
  type IThread,
  type ThreadHistory,
  getChainlitStore
} from '@chainlit-ng/react-client';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ThreadList } from '@/components/LeftSidebar/ThreadList';
import { SidebarProvider } from '@/components/ui/sidebar';

vi.mock('@chainlit-ng/react-client', async () => ({
  ...(await vi.importActual<typeof import('@chainlit-ng/react-client')>(
    '@chainlit-ng/react-client'
  )),
  useConfig: () => ({
    config: { threadResumable: true, dataPersistence: true }
  })
}));

vi.mock('@/components/i18n', () => ({
  Translator: ({ path }: { path: string }) => <span>{path}</span>
}));

vi.mock('@/hooks/use-mobile', () => ({
  useIsMobile: () => false
}));

vi.mock('@/components/LeftSidebar/ThreadOptions', () => ({
  default: ({ onDelete }: { onDelete: () => void }) => (
    <button
      type="button"
      onClick={(event) => {
        event.preventDefault();
        onDelete();
      }}
    >
      Delete thread
    </button>
  )
}));

const thread: IThread = {
  id: 'thread-to-delete',
  name: 'Saved conversation',
  createdAt: '2026-01-01T00:00:00Z',
  steps: []
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="path">{location.pathname}</output>;
}

function path() {
  return screen.getByTestId('path').textContent;
}

function setup({
  currentThreadId,
  idToResume
}: {
  currentThreadId?: string;
  idToResume?: string;
}) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const deleteThread = vi.spyOn(client, 'deleteThread');
  const history: ThreadHistory = {
    threads: [thread],
    timeGroupedThreads: { Today: [thread] }
  };
  const store = getChainlitStore(client);
  store.setState({
    currentThreadId,
    threadIdToResume: idToResume,
    threadHistory: history
  });

  render(
    <ChainlitContext.Provider value={client}>
      <MemoryRouter
        initialEntries={[`/thread/${currentThreadId ?? thread.id}`]}
      >
        <SidebarProvider>
          <ThreadList
            threadHistory={history}
            isFetching={false}
            isLoadingMore={false}
          />
          <LocationProbe />
        </SidebarProvider>
      </MemoryRouter>
    </ChainlitContext.Provider>
  );

  return { deleteThread, store };
}

function confirmDeletion() {
  fireEvent.click(screen.getByRole('button', { name: 'Delete thread' }));
  fireEvent.click(screen.getByText('common.actions.confirm'));
}

describe('ThreadList deletion', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([
    ['the current thread', { currentThreadId: thread.id }],
    ['the thread being resumed', { idToResume: thread.id }]
  ])('leaves %s before deleting it', async (_description, state) => {
    const { deleteThread, store } = setup(state);
    deleteThread.mockImplementation(() => new Promise(() => {}));

    confirmDeletion();

    expect(path()).toBe('/');
    expect(store.getState().currentThreadId).toBeUndefined();
    expect(store.getState().threadIdToResume).toBeUndefined();
    expect(store.getState().threadHistory?.threads).toEqual([thread]);
    expect(deleteThread).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(deleteThread).toHaveBeenCalledWith(thread.id);
  });

  it('removes the deleted active thread from history', async () => {
    const { deleteThread, store } = setup({ currentThreadId: thread.id });
    deleteThread.mockResolvedValue({ success: true });

    confirmDeletion();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(deleteThread).toHaveBeenCalledWith(thread.id);
    expect(store.getState().threadHistory?.threads).toEqual([]);
    expect(path()).toBe('/');
  });

  it('keeps another active thread until deleting the historical thread succeeds', async () => {
    const { deleteThread, store } = setup({
      currentThreadId: 'other-thread'
    });
    let resolveDeletion!: (value: unknown) => void;
    deleteThread.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDeletion = resolve;
        })
    );

    confirmDeletion();

    expect(deleteThread).toHaveBeenCalledWith(thread.id);
    expect(path()).toBe('/thread/other-thread');
    expect(store.getState().currentThreadId).toBe('other-thread');
    expect(store.getState().threadHistory?.threads).toEqual([thread]);

    await act(async () => {
      resolveDeletion({ success: true });
    });

    expect(path()).toBe('/thread/other-thread');
    expect(store.getState().currentThreadId).toBe('other-thread');
    expect(store.getState().threadHistory?.threads).toEqual([]);
  });

  it('preserves the active thread and history when deleting another thread fails', async () => {
    const { deleteThread, store } = setup({
      currentThreadId: 'other-thread'
    });
    let rejectDeletion!: (reason: unknown) => void;
    deleteThread.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectDeletion = reject;
        })
    );

    confirmDeletion();
    await act(async () => {
      rejectDeletion(new Error('Deletion failed'));
    });

    expect(path()).toBe('/thread/other-thread');
    expect(store.getState().currentThreadId).toBe('other-thread');
    expect(store.getState().threadHistory?.threads).toEqual([thread]);
  });

  it('returns to the active thread and re-requests its resume when deletion fails', async () => {
    const { deleteThread, store } = setup({ currentThreadId: thread.id });
    let rejectDeletion!: (reason: unknown) => void;
    deleteThread.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectDeletion = reject;
        })
    );

    confirmDeletion();
    expect(path()).toBe('/');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    await act(async () => {
      rejectDeletion(new Error('Deletion failed'));
    });

    expect(path()).toBe(`/thread/${thread.id}`);
    expect(store.getState().threadIdToResume).toBe(thread.id);
  });
});
