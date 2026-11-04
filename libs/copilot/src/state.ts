import { v4 as uuidv4 } from 'uuid';
import { create } from 'zustand';

const COPILOT_THREAD_ID_KEY = 'chainlit-copilot-thread-id';
export const COPILOT_THREAD_CHANGED_EVENT_KEY =
  'chainlit-copilot-thread-changed';

export class CopilotThreadChangedEventParams {
  newThreadId?: string;
}

interface CopilotState {
  copilotThreadId: string;
  initialized: boolean;
  initialize: () => void;
  setCopilotThreadId: (threadId: string) => void;
}

export const useCopilotStore = create<CopilotState>((set, get) => ({
  copilotThreadId: '',
  initialized: false,
  initialize: () => {
    if (get().initialized) return;

    let threadId: string;
    try {
      const stored = localStorage.getItem(COPILOT_THREAD_ID_KEY);
      threadId = stored ? JSON.parse(stored) : uuidv4();
      if (!stored) {
        localStorage.setItem(COPILOT_THREAD_ID_KEY, JSON.stringify(threadId));
      }
    } catch {
      threadId = uuidv4();
    }

    set({ copilotThreadId: threadId, initialized: true });
  },
  setCopilotThreadId: (threadId) => {
    localStorage.setItem(COPILOT_THREAD_ID_KEY, JSON.stringify(threadId));
    set({ copilotThreadId: threadId, initialized: true });
  }
}));

export const getChainlitCopilotThreadId = () => {
  const threadId = localStorage.getItem(COPILOT_THREAD_ID_KEY);
  return threadId ? JSON.parse(threadId) : null;
};

export const clearChainlitCopilotThreadId = (newThreadId?: string) => {
  window.dispatchEvent(
    new CustomEvent<CopilotThreadChangedEventParams>(
      COPILOT_THREAD_CHANGED_EVENT_KEY,
      {
        detail: { newThreadId }
      }
    )
  );
};
