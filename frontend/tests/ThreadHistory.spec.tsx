import {
  ChainlitAPI,
  ChainlitContext,
  type IThread,
  beginThreadHistoryRequest,
  getChainlitStore,
  getThreadHistoryRefresher
} from '@chainlit-ng/react-client';
import { act, fireEvent, render } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ThreadHistory } from '@/components/LeftSidebar/ThreadHistory';
import { SidebarProvider } from '@/components/ui/sidebar';

vi.mock('@/components/LeftSidebar/ThreadList', () => ({
  ThreadList: ({
    isFetching,
    isLoadingMore
  }: {
    isFetching: boolean;
    isLoadingMore: boolean;
  }) => (
    <output data-testid="thread-list-state">
      {`${isFetching ? 'fetching' : 'idle'}:${
        isLoadingMore ? 'loading-more' : 'idle'
      }`}
    </output>
  )
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

function loadingState() {
  return document.querySelector('[data-testid="thread-list-state"]')
    ?.textContent;
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
  firstFetch,
  fetchResults
}: {
  threads?: IThread[];
  firstFetch?: Promise<ListThreadsResult>;
  fetchResults?: Promise<ListThreadsResult>[];
} = {}) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const pending = new Promise<ListThreadsResult>(() => {});
  const listThreads = vi.spyOn(client, 'listThreads');
  if (fetchResults) {
    fetchResults.forEach((result) => listThreads.mockReturnValueOnce(result));
    listThreads.mockReturnValue(pending);
  } else if (firstFetch) {
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
    const { client, listThreads, store } = setup({ firstFetch: promise });

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
    // A stale completion must not clear loading and trigger another fetch.
    expect(listThreads).toHaveBeenCalledTimes(1);
    expect(loadingState()).toBe('fetching:idle');
  });

  it('keeps loading owned by a refresh that superseded the initial fetch', async () => {
    const initial = deferred<ListThreadsResult>();
    const refresh = deferred<ListThreadsResult>();
    const { client, listThreads, store } = setup({
      fetchResults: [initial, refresh]
    });

    const refresher = getThreadHistoryRefresher(client);
    expect(refresher).toBeTruthy();

    let refreshRun!: Promise<void>;
    act(() => {
      refreshRun = refresher!();
    });

    await act(async () => {
      initial.resolve({
        pageInfo: { hasNextPage: false },
        data: [{ ...thread, id: 'stale-thread' }]
      });
      await initial.promise;
    });

    // The stale initial fetch must not unlock loading owned by the refresh.
    expect(store.getState().threadHistory?.threads).toBeUndefined();
    expect(loadingState()).toBe('fetching:idle');
    expect(listThreads).toHaveBeenCalledTimes(2);

    await act(async () => {
      refresh.resolve({
        pageInfo: { hasNextPage: false },
        data: [{ ...thread, id: 'fresh-thread' }]
      });
      await refreshRun;
    });

    expect(
      store.getState().threadHistory?.threads?.map((item) => item.id)
    ).toEqual(['fresh-thread']);
    expect(loadingState()).toBe('idle:idle');
    expect(listThreads).toHaveBeenCalledTimes(2);
  });

  it('does not let a paginated page overwrite a refresh that arrived while it was in flight', async () => {
    const page = deferred<ListThreadsResult>();
    const refresh = deferred<ListThreadsResult>();
    const { client, listThreads, store } = setup({
      threads: [thread],
      fetchResults: [page, refresh]
    });

    act(() => {
      store.setState({
        threadHistory: {
          threads: [thread],
          timeGroupedThreads: { Today: [thread] },
          pageInfo: { hasNextPage: true, endCursor: 'cursor-1' }
        }
      });
    });

    // Scrolling to the bottom starts a pagination request.
    act(() => {
      fireEvent.scroll(document.querySelector('[data-sidebar="content"]')!);
    });
    expect(listThreads).toHaveBeenCalledTimes(1);

    // A refresh arrives while the pagination request is still pending.
    const refresher = getThreadHistoryRefresher(client);
    expect(refresher).toBeTruthy();
    let refreshRun!: Promise<void>;
    act(() => {
      refreshRun = refresher!();
    });
    expect(listThreads).toHaveBeenCalledTimes(2);

    // The stale page resolves first and must not be written to history.
    await act(async () => {
      page.resolve({
        pageInfo: { hasNextPage: false },
        data: [{ ...thread, id: 'stale-page-thread' }]
      });
      await page.promise;
    });

    expect(
      store.getState().threadHistory?.threads?.map((item) => item.id)
    ).toEqual(['thread-1']);

    // The refresh wins and replaces the list with the newest first page.
    await act(async () => {
      refresh.resolve({
        pageInfo: { hasNextPage: false },
        data: [{ ...thread, id: 'fresh-thread' }]
      });
      await refreshRun;
    });

    expect(
      store.getState().threadHistory?.threads?.map((item) => item.id)
    ).toEqual(['fresh-thread']);
    expect(loadingState()).toBe('idle:idle');
  });
});
