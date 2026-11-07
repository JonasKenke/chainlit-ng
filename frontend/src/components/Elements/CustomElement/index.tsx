import { MessageContext } from 'contexts/MessageContext';
import {
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState
} from 'react';
import { Runner } from 'react-runner';
import { v4 as uuidv4 } from 'uuid';

import {
  ChainlitContext,
  IAction,
  ICustomElement,
  IElement,
  useAuth,
  useChainlitStore,
  useChatInteract
} from '@chainlit/react-client';

import Alert from '@/components/Alert';

import Imports from './Imports';
import * as Renderer from './Renderer';
import { type LoadedCustomElement, loadCustomElement } from './loader';

const CustomElement = memo(function ({ element }: { element: ICustomElement }) {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const { sendMessage } = useChatInteract();
  const { user } = useAuth();
  const { askUser } = useContext(MessageContext);

  const [loaded, setLoaded] = useState<LoadedCustomElement>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    loadCustomElement({
      name: element.name,
      imports: { ...Imports, '@/components/renderer': Renderer },
      apiClient
    })
      .then(setLoaded)
      .catch((err) => setError(String(err)));
  }, [element.name, apiClient]);

  const updateElement = useCallback(
    (nextProps: Record<string, unknown>) => {
      if (!sessionId) return;
      const nextElement: IElement = { ...element, props: nextProps };
      return apiClient.updateElement(nextElement, sessionId);
    },
    [element, sessionId, apiClient]
  );

  const deleteElement = useCallback(() => {
    if (!sessionId) return;
    return apiClient.deleteElement(element, sessionId);
  }, [element, sessionId, apiClient]);

  const callAction = useCallback(
    (action: IAction) => {
      if (!sessionId) return;
      return apiClient.callAction(action, sessionId);
    },
    [sessionId, apiClient]
  );

  const sendUserMessage = useCallback(
    (message: string, command?: string) => {
      return sendMessage({
        threadId: '',
        id: uuidv4(),
        name: user?.identifier || 'User',
        type: 'user_message',
        output: message,
        createdAt: new Date().toISOString(),
        metadata: { location: window.location.href },
        command
      });
    },
    [sendMessage, user]
  );

  const submitElement = useCallback(
    (props: Record<string, unknown>) => {
      if (
        askUser?.spec.type === 'element' &&
        askUser.spec.step_id === element.forId
      ) {
        askUser.callback({ ...props, submitted: true });
      }
    },
    [askUser, element.forId]
  );

  const cancelElement = useCallback(() => {
    if (
      askUser?.spec.type === 'element' &&
      askUser.spec.step_id === element.forId
    ) {
      askUser.callback({ submitted: false });
    }
  }, [askUser, element.forId]);

  const props = useMemo(() => {
    return JSON.parse(JSON.stringify(element.props));
  }, [element.props]);

  if (error) return <Alert variant="error">{error}</Alert>;
  if (!loaded) return null;

  return (
    <div className={`${element.display}-custom flex flex-col flex-grow`}>
      <Runner
        code={loaded.code}
        scope={{
          import: loaded.imports,
          props,
          apiClient,
          updateElement,
          deleteElement,
          callAction,
          sendUserMessage,
          submitElement,
          cancelElement
        }}
        onRendered={(error) => setError(error?.message)}
      />
    </div>
  );
});

export default CustomElement;
