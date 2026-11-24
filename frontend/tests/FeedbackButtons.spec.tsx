import {
  ChainlitAPI,
  ChainlitContext,
  type IStep
} from '@chainlit-ng/react-client';
import { render, screen } from '@testing-library/react';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { describe, expect, it, vi } from 'vitest';

import { FeedbackButtons } from '@/components/chat/Messages/Message/Buttons/FeedbackButtons';

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => <span>{path}</span>,
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

const client = new ChainlitAPI('http://localhost:8000', 'webapp');

const message: IStep = {
  id: 'message-1',
  threadId: 'thread-1',
  type: 'assistant_message',
  name: 'Assistant',
  output: 'Hello',
  createdAt: '2026-01-01T00:00:00Z'
};

function renderWithFeedback(showFeedbackButtons: boolean) {
  return (
    <ChainlitContext.Provider value={client}>
      <MessageContext.Provider
        value={{ ...defaultMessageContext, showFeedbackButtons }}
      >
        <FeedbackButtons message={message} />
      </MessageContext.Provider>
    </ChainlitContext.Provider>
  );
}

describe('FeedbackButtons', () => {
  it('keeps its hook order when feedback toggles on', () => {
    const { rerender } = render(renderWithFeedback(false));
    expect(screen.queryAllByRole('button')).toHaveLength(0);

    rerender(renderWithFeedback(true));

    expect(screen.getAllByRole('button').length).toBeGreaterThan(0);
  });

  it('keeps its hook order when feedback toggles off', () => {
    const { rerender } = render(renderWithFeedback(true));
    expect(screen.getAllByRole('button').length).toBeGreaterThan(0);

    rerender(renderWithFeedback(false));

    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
