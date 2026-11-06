import { useEffect } from 'react';
import { useLocation, useParams } from 'react-router-dom';

import Page from 'pages/Page';

import {
  useChainlitStore,
  useChatInteract,
  useChatMessages,
  useConfig,
  useSetChainlitState
} from '@chainlit/react-client';

import AutoResumeThread from '@/components/AutoResumeThread';
import { Loader } from '@/components/Loader';
import { ReadOnlyThread } from '@/components/ReadOnlyThread';
import Chat from '@/components/chat';

export default function ThreadPage() {
  const { id } = useParams();
  const location = useLocation();
  const { config } = useConfig();

  const setThreadHistory = useSetChainlitState('threadHistory');
  const { threadId } = useChatMessages();
  const unavailableThreadId = useChainlitStore(
    (state) => state.unavailableThreadId
  );
  const { clear } = useChatInteract();

  const isCurrentThread = threadId === id;
  const isUnavailableThread = !!id && unavailableThreadId === id;

  useEffect(() => {
    setThreadHistory((prev) => {
      if (prev?.currentThreadId === id) return prev;
      return { ...prev, currentThreadId: id };
    });
  }, [id]);

  useEffect(() => {
    if (!id && unavailableThreadId) clear();
  }, [id, unavailableThreadId, clear]);

  const isSharedRoute = location.pathname.startsWith('/share/');

  return (
    <Page>
      <>
        {isSharedRoute || isUnavailableThread ? (
          <ReadOnlyThread id={id!} />
        ) : null}
        {config?.threadResumable && !isSharedRoute && !isUnavailableThread ? (
          <AutoResumeThread id={id!} />
        ) : null}
        {config?.threadResumable && !isSharedRoute && !isUnavailableThread ? (
          isCurrentThread ? (
            <Chat />
          ) : (
            <div className="flex flex-grow items-center justify-center">
              <Loader className="!size-6" />
            </div>
          )
        ) : null}
        {config &&
        !config.threadResumable &&
        !isSharedRoute &&
        !isUnavailableThread ? (
          isCurrentThread ? (
            <Chat />
          ) : (
            <ReadOnlyThread id={id!} />
          )
        ) : null}
      </>
    </Page>
  );
}
