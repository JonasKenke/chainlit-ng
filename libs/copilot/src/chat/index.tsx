import { useEffect, useRef } from 'react';

import {
  useChatInteract,
  useChatSession,
  useSetChainlitState
} from '@chainlit/react-client';

import { useCopilotStore } from '../state';
import ChatBody from './body';

export default function ChatWrapper() {
  const { connect, session, idToResume } = useChatSession();
  const { sendMessage } = useChatInteract();
  const copilotThreadId = useCopilotStore((state) => state.copilotThreadId);
  const initializeCopilotThreadId = useCopilotStore(
    (state) => state.initialize
  );
  const setThreadIdToResume = useSetChainlitState('threadIdToResume');
  const hasConnected = useRef<boolean>(false);

  useEffect(() => {
    initializeCopilotThreadId();
  }, [initializeCopilotThreadId]);
  const lastConnectedThreadId = useRef<string | null>(null);

  useEffect(() => {
    if (!copilotThreadId) {
      return;
    }

    setThreadIdToResume(copilotThreadId);
  }, [copilotThreadId, setThreadIdToResume]);

  useEffect(() => {
    if (
      copilotThreadId &&
      lastConnectedThreadId.current &&
      copilotThreadId !== lastConnectedThreadId.current &&
      hasConnected.current
    ) {
      if (session?.socket?.connected) {
        session.socket.disconnect();
      }
      hasConnected.current = false;
      lastConnectedThreadId.current = null;
    }
  }, [copilotThreadId]);

  useEffect(() => {
    if (!copilotThreadId || !idToResume || copilotThreadId !== idToResume) {
      return;
    }

    if (hasConnected.current) {
      return;
    }

    hasConnected.current = true;
    lastConnectedThreadId.current = copilotThreadId;
    connect({
      // @ts-expect-error window typing
      transports: window.transports,
      userEnv: {}
    });
  }, [copilotThreadId, idToResume, connect]);

  useEffect(() => {
    // @ts-expect-error is not a valid prop
    window.sendChainlitMessage = sendMessage;
  }, [sendMessage]);

  return <ChatBody />;
}
