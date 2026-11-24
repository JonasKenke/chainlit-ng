import {
  ChainlitAPI,
  ChainlitContext,
  type IThread,
  beginThreadHistoryRequest,
  getChainlitStore
} from '@chainlit-ng/react-client';
import { act, render } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ThreadHistory } from '@/components/LeftSidebar/ThreadHistory';
import { SidebarProvider } from '@/components/ui/sidebar';

vi.mock('@/components/LeftSidebar/ThreadList', () => ({
  ThreadList: () => null
}));

vi.mock('@/hooks/use-mobile', () => ({
  useIsMobile: () => false
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="path">{location.pathname}</output>;
}

function path() {
  return document.querySelector('[data-testid="path"]')?.textContent;
}

const thread: IThread = {
  id: 'thread-1',
  name: 'Existing conversation',
  createdAt: '2026-01-01T00:00:00Z',
  steps: []
};

type ListThreadsResult = Awaited<ReturnType<ChainlitAPI['listThreads']>>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function setup({
  threads,
  firstFetch
}: {
  threads?: IThread[];
  firstFetch?: Promise<ListThreadsResult>;
} = {}) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const pending = new Promise<ListThreadsResult>(() => {});
  const listThreads = vi.spyOn(client, 'listThreads');
  if (firstFetch) {
    listThreads.mockReturnValueOnce(firstFetch).mockReturnValue(pending);
  } else {
    listThreads.mockReturnValue(pending);
  }
  const store = getChainlitStore(client);
  if (threads) {
    store.setState({
      threadHistory: {
        threads,
        timeGroupedThreads: { Today: threads }
      }
    });
  }

  render(
    <ChainlitContext.Provider value={client}>
      <MemoryRouter initialEntries={['/']}>
        <SidebarProvider>
          <ThreadHistory />
          <LocationProbe />
        </SidebarProvider>
      </MemoryRouter>
    </ChainlitContext.Provider>
  );

  return { client, listThreads, store };
}

describe('ThreadHistory navigation', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('navigates to the active thread without waiting for history', async () => {
    const { listThreads, store } = setup();

    act(() => {
      store.setState({
        currentThreadId: 'thread-1',
        firstUserInteraction: 'message'
      });
    });

    expect(path()).toBe('/thread/thread-1');
    expect(listThreads).toHaveBeenCalledTimes(1);
  });

  it('does not fetch history again when the first interaction arrives', () => {
    const { listThreads, store } = setup({ threads: [] });

    act(() => {
      store.setState({
        currentThreadId: 'thread-1',
        firstUserInteraction: 'message'
      });
    });

    expect(path()).toBe('/thread/thread-1');
    expect(listThreads).not.toHaveBeenCalled();
  });

  it('ignores an initial fetch that a newer request superseded', async () => {
    const { promise, resolve } = deferred<ListThreadsResult>();
    const { client, store } = setup({ firstFetch: promise });

    act(() => {
      beginThreadHistoryRequest(client);
    });

    await act(async () => {
      resolve({
        pageInfo: { hasNextPage: false },
        data: [{ ...thread, id: 'stale-thread' }]
      });
      await promise;
    });

    expect(store.getState().threadHistory?.threads).toBeUndefined();
  });
});
