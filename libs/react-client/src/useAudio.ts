import { useCallback } from 'react';

import { useChainlitState, useChainlitStore } from './state';
import { useChatInteract } from './useChatInteract';

const useAudio = () => {
  const [audioConnection, setAudioConnection] =
    useChainlitState('audioConnection');
  const wavRecorder = useChainlitStore((state) => state.wavRecorder);
  const wavStreamPlayer = useChainlitStore((state) => state.wavStreamPlayer);
  const isAiSpeaking = useChainlitStore((state) => state.isAiSpeaking);

  const { startAudioStream, endAudioStream } = useChatInteract();

  const startConversation = useCallback(async () => {
    setAudioConnection('connecting');
    await startAudioStream();
  }, [startAudioStream]);

  const endConversation = useCallback(async () => {
    setAudioConnection('off');
    await wavRecorder.end();
    await wavStreamPlayer.interrupt();
    await endAudioStream();
  }, [endAudioStream, wavRecorder, wavStreamPlayer]);

  return {
    startConversation,
    endConversation,
    audioConnection,
    isAiSpeaking,
    wavRecorder,
    wavStreamPlayer
  };
};

export { useAudio };
