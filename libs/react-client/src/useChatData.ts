import { useChainlitStore } from './state';

export interface IToken {
  id: number | string;
  token: string;
  isSequence: boolean;
  isInput: boolean;
}

const useChatData = () => {
  const loading = useChainlitStore((state) => state.loading);
  const elements = useChainlitStore((state) => state.elements);
  const tasklists = useChainlitStore((state) => state.tasklists);
  const actions = useChainlitStore((state) => state.actions);
  const session = useChainlitStore((state) => state.session);
  const askUser = useChainlitStore((state) => state.askUser);
  const callFn = useChainlitStore((state) => state.callFn);
  const chatSettingsInputs = useChainlitStore(
    (state) => state.chatSettingsInputs
  );
  const chatSettingsValue = useChainlitStore(
    (state) => state.chatSettingsValue
  );
  const chatSettingsDefaultValue = useChainlitStore(
    (state) => state.chatSettingsDefaultValue
  );

  const connected = session?.socket.connected && !session?.error;
  const disabled =
    !connected ||
    loading ||
    askUser?.spec.type === 'file' ||
    askUser?.spec.type === 'action' ||
    askUser?.spec.type === 'element';

  return {
    actions,
    askUser,
    callFn,
    chatSettingsDefaultValue,
    chatSettingsInputs,
    chatSettingsValue,
    connected,
    disabled,
    elements,
    error: session?.error,
    loading,
    tasklists
  };
};

export { useChatData };
