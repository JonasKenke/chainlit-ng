import { useEffect } from 'react';

import Page from 'pages/Page';

import { useChainlitStore, useChatInteract } from '@chainlit/react-client';

import Chat from '@/components/chat';

export default function Home() {
  const unavailableThreadId = useChainlitStore(
    (state) => state.unavailableThreadId
  );
  const { clear } = useChatInteract();

  useEffect(() => {
    if (unavailableThreadId) clear();
  }, [unavailableThreadId, clear]);

  return (
    <Page>
      <Chat />
    </Page>
  );
}
