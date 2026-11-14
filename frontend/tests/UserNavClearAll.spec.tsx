import {
  ChainlitAPI,
  ChainlitContext,
  type IThread,
  type ThreadHistory,
  getChainlitStore
} from '@chainlit-ng/react-client';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor
} from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import UserNav from '@/components/header/UserNav';

const { mockConfig } = vi.hoisted(() => ({
  mockConfig: { threadClearAll: true } as Record<string, unknown>
}));

vi.mock('@chainlit-ng/react-client', async () => ({
  ...(await vi.importActual<typeof import('@chainlit-ng/react-client')>(
    '@chainlit-ng/react-client'
  )),
  useConfig: () => ({ config: mockConfig }),
  useAuth: () => ({
    user: { identifier: 'alice', display_name: 'Alice', metadata: {} },
    logout: vi.fn()
  })
}));

vi.mock('@/components/i18n', () => ({
  Translator: ({ path }: { path: string }) => <span>{path}</span>,
  useTranslation: () => ({ t: (path: string) => path })
}));

const thread: IThread = {
  id: 'thread-to-clear',
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
} = {}) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const deleteAllThreads = vi.spyOn(client, 'deleteAllThreads');
  const listThreads = vi.spyOn(client, 'listThreads');
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
        initialEntries={[currentThreadId ? `/thread/${currentThreadId}` : '/']}
      >
        <UserNav />
        <LocationProbe />
      </MemoryRouter>
    </ChainlitContext.Provider>
  );

  return { deleteAllThreads, listThreads, store };
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole('button'), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse'
  });
}

function openDialog() {
  openMenu();
  fireEvent.click(screen.getByText('navigation.user.menu.clearAll'));
}

function confirmClearAll() {
  openDialog();
  fireEvent.click(screen.getByText('threadHistory.clearAll.confirm'));
}

describe('UserNav clear all chats', () => {
  beforeEach(() => {
    mockConfig.threadClearAll = true;
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([false, undefined])(
    'hides the action when threadClearAll is %s',
    (threadClearAll) => {
      mockConfig.threadClearAll = threadClearAll;
      setup();

      openMenu();

      expect(
        screen.queryByText('navigation.user.menu.clearAll')
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText('threadHistory.clearAll.title')
      ).not.toBeInTheDocument();
    }
  );

  it('shows a destructive confirmation without requesting on cancel', () => {
    const { deleteAllThreads } = setup();

    openDialog();

    expect(
      screen.getByText('threadHistory.clearAll.title')
    ).toBeInTheDocument();
    expect(
      screen.getByText('threadHistory.clearAll.description')
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('common.actions.cancel'));
    expect(deleteAllThreads).not.toHaveBeenCalled();
  });

  it('leaves the active thread before requesting deletion', async () => {
    const { deleteAllThreads, store } = setup({
      currentThreadId: thread.id
    });
    deleteAllThreads.mockImplementation(() => new Promise(() => {}));

    confirmClearAll();

    expect(path()).toBe('/');
    expect(store.getState().currentThreadId).toBeUndefined();
    expect(deleteAllThreads).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(deleteAllThreads).toHaveBeenCalled();
  });

  it('leaves the thread being resumed before requesting deletion', async () => {
    const { deleteAllThreads, store } = setup({ idToResume: thread.id });
    deleteAllThreads.mockImplementation(() => new Promise(() => {}));

    confirmClearAll();

    expect(path()).toBe('/');
    expect(store.getState().threadIdToResume).toBeUndefined();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(deleteAllThreads).toHaveBeenCalled();
  });

  it('empties the history on success', async () => {
    vi.useRealTimers();
    const { deleteAllThreads, store } = setup();
    deleteAllThreads.mockResolvedValue({ success: true, deleted: 1 });

    confirmClearAll();

    await waitFor(() => {
      expect(deleteAllThreads).toHaveBeenCalled();
      expect(store.getState().threadHistory?.threads).toEqual([]);
      expect(store.getState().threadHistory?.timeGroupedThreads).toEqual({});
    });
  });

  it('refetches the history on error', async () => {
    vi.useRealTimers();
    const { deleteAllThreads, listThreads, store } = setup();
    const restored: IThread = { ...thread, id: 'surviving-thread' };
    deleteAllThreads.mockRejectedValue(new Error('Deletion failed'));
    listThreads.mockResolvedValue({
      pageInfo: { hasNextPage: false },
      data: [restored]
    });

    confirmClearAll();

    await waitFor(() => {
      expect(deleteAllThreads).toHaveBeenCalled();
      expect(listThreads).toHaveBeenCalled();
      expect(store.getState().threadHistory?.threads).toEqual([restored]);
    });
    expect(path()).toBe('/');
  });
});
