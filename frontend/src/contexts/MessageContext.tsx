import { createContext } from 'react';

import { IMessageContext } from 'types/messageContext';

const defaultMessageContext = {
  highlightedMessage: null,
  loading: false,
  editable: false,
  onElementRefClick: undefined,
  onFeedbackUpdated: undefined,
  showFeedbackButtons: true,
  onError: () => undefined,
  uiName: '',
  cot: 'hidden' as const,
  cotDisplay: 'list' as const
};

const MessageContext = createContext<IMessageContext>(defaultMessageContext);

export { MessageContext, defaultMessageContext };
