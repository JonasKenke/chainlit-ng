import {
  ChainlitContext,
  THREAD_HISTORY_PAGE_SIZE,
  beginThreadHistoryPage,
  beginThreadHistoryRequest,
  finishThreadHistoryRequest,
  isLatestThreadHistoryRequest,
  registerThreadHistoryRefresher,
  unregisterThreadHistoryRefresher,
  useChainlitState,
  useChatMessages
} from '@chainlit-ng/react-client';
import { uniqBy } from 'lodash';
import { useContext, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  SidebarContent,
  SidebarGroup,
  SidebarMenu
} from '@/components/ui/sidebar';

import { ThreadList } from './ThreadList';

const BATCH_SIZE = THREAD_HISTORY_PAGE_SIZE;
let _scrollTop = 0;

export function ThreadHistory() {
  const navigate = useNavigate();
  const scrollRef = useRef<HTMLDivElement>(null);
  const apiClient = useContext(ChainlitContext);
  const { firstInteraction, messages, threadId } = useChatMessages();
  const [threadHistory, setThreadHistory] = useChainlitState('threadHistory');
  const [error, setError] = useState<string>();
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [isFetching, setIsFetching] = useState(false);
  const [shouldLoadMore, setShouldLoadMore] = useState(false);
  const prevMessageCountRef = useRef(0);

  // Restore scroll position
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = _scrollTop;
    }
  }, []);

  // Handle first interaction
  useEffect(() => {
    const handleFirstInteraction = () => {
      if (!firstInteraction) return;

      const isActualResume =
        firstInteraction === 'resume' &&
        messages[0]?.output.toLowerCase() !== 'resume';

      if (isActualResume) return;

      // Navigate right away; the mount fetch and `thread_list_updated`
      // events keep history current without blocking the chat view.
      const currentPage = new URL(window.location.href);
      if (threadId && currentPage.pathname === '/') {
        navigate(`/thread/${threadId}`);
      }
    };

    handleFirstInteraction();
  }, [firstInteraction]);

  // Reorder thread to top when a new message is sent in the current thread
  useEffect(() => {
    const currentCount = messages.length;
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = currentCount;

    if (
      threadId &&
      currentCount > prevCount &&
      prevCount > 0 &&
      threadHistory?.threads
    ) {
      const lastMessage = messages[currentCount - 1];
      if (lastMessage?.type === 'user_message') {
        setThreadHistory((prev) => {
          if (!prev?.threads) return prev;
          const threadIndex = prev.threads.findIndex((t) => t.id === threadId);
          if (threadIndex <= 0) return prev; // Already at top or not found
          const updatedThreads = [...prev.threads];
          updatedThreads[threadIndex] = {
            ...updatedThreads[threadIndex],
            createdAt: new Date().toISOString()
          };
          return { ...prev, threads: updatedThreads };
        });
      }
    }
  }, [messages.length, threadId]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollHeight, clientHeight, scrollTop } = scrollRef.current;
    const atBottom = scrollTop + clientHeight >= scrollHeight - 10;

    _scrollTop = scrollTop;
    setShouldLoadMore(atBottom);
  };

  const fetchThreads = async (cursor?: string | number) => {
    const isFirstPage = !cursor;
    // First-page requests (mount and refresh events) supersede everything and
    // own the loading state. Pagination only extends the current page, so it
    // captures the generation without bumping it and waits for any refresh.
    const requestId = isFirstPage
      ? beginThreadHistoryRequest(apiClient)
      : beginThreadHistoryPage(apiClient);

    if (requestId === null) return;

    try {
      setIsLoadingMore(!isFirstPage);
      setIsFetching(isFirstPage);

      const { pageInfo, data } = await apiClient.listThreads(
        { first: BATCH_SIZE, cursor },
        {}
      );

      // A refresh event, reconnect, or newer request may have started while
      // this request was in flight. Never let a stale response win.
      if (!isLatestThreadHistoryRequest(apiClient, requestId)) return;

      setError(undefined);

      // Prevent duplicate threads
      const allThreads = uniqBy(
        cursor ? threadHistory?.threads?.concat(data) : data,
        'id'
      );

      if (allThreads) {
        setThreadHistory((prev) => ({
          ...prev,
          pageInfo,
          threads: allThreads
        }));
      }
    } catch (err) {
      if (!isLatestThreadHistoryRequest(apiClient, requestId)) return;
      setError(err instanceof Error ? err.message : 'Unknown error occurred');
    } finally {
      // Only the request that still owns the state may clear it. Otherwise a
      // stale completion unlocks pagination or triggers another initial fetch.
      if (isLatestThreadHistoryRequest(apiClient, requestId)) {
        if (isFirstPage) finishThreadHistoryRequest(apiClient, requestId);
        setShouldLoadMore(false);
        setIsLoadingMore(false);
        setIsFetching(false);
      }
    }
  };

  // Let session refresh events use this component's fetch so loading state and
  // pagination stay coordinated with the single in-flight first-page request.
  const fetchThreadsRef = useRef(fetchThreads);
  fetchThreadsRef.current = fetchThreads;

  useEffect(() => {
    const refresh = () => fetchThreadsRef.current();
    registerThreadHistoryRefresher(apiClient, refresh);
    return () => unregisterThreadHistoryRefresher(apiClient, refresh);
  }, [apiClient]);

  // Initial fetch
  useEffect(() => {
    if (!isFetching && !threadHistory?.threads && !error) {
      fetchThreads();
    }
  }, [isFetching, threadHistory, error]);

  // Handle infinite scroll
  useEffect(() => {
    if (threadHistory?.pageInfo) {
      const { hasNextPage, endCursor } = threadHistory.pageInfo;

      if (shouldLoadMore && !isLoadingMore && hasNextPage && endCursor) {
        fetchThreads(endCursor);
      }
    }
  }, [shouldLoadMore, isLoadingMore, threadHistory]);

  return (
    <SidebarContent onScroll={handleScroll} ref={scrollRef}>
      <SidebarGroup>
        <SidebarMenu>
          {threadHistory ? (
            <div id="thread-history" className="flex-grow">
              <ThreadList
                threadHistory={threadHistory}
                error={error}
                isFetching={isFetching}
                isLoadingMore={isLoadingMore}
              />
            </div>
          ) : null}
        </SidebarMenu>
      </SidebarGroup>
    </SidebarContent>
  );
}
