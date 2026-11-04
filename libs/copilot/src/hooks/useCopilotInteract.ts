import { useCallback } from 'react';
import { v4 as uuidv4 } from 'uuid';

import { useChatInteract } from '@chainlit/react-client';

import { useCopilotStore } from '../state';

export const useCopilotInteract = () => {
  const chatInteract = useChatInteract();
  const setCopilotThreadId = useCopilotStore(
    (state) => state.setCopilotThreadId
  );
  const startNewChat = useCallback(
    (newThreadId?: string) => {
      chatInteract.clear();
      setCopilotThreadId(newThreadId || uuidv4());
    },
    [chatInteract, setCopilotThreadId]
  );

  return {
    ...chatInteract,
    clear: startNewChat,
    startNewChat
  };
};
