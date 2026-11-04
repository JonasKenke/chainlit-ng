import { useChainlitStore } from './state';

const useChatMessages = () => {
  const messages = useChainlitStore((state) => state.messages);
  const firstInteraction = useChainlitStore(
    (state) => state.firstUserInteraction
  );
  const threadId = useChainlitStore((state) => state.currentThreadId);

  return {
    threadId,
    messages,
    firstInteraction
  };
};

export { useChatMessages };
