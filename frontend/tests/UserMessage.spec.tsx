import { fireEvent, render, screen } from '@testing-library/react';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IStep } from '@chainlit/react-client';

import UserMessage from '@/components/chat/Messages/Message/UserMessage';

const { editMessage, setMessages, toastError } = vi.hoisted(() => ({
  editMessage: vi.fn(),
  setMessages: vi.fn(),
  toastError: vi.fn()
}));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

vi.mock('components/i18n', () => ({
  Translator: ({ path }: { path: string }) => path,
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

vi.mock('@/components/chat/Messages/Message/Buttons/BookmarkButton', () => ({
  BookmarkButton: () => null
}));

vi.mock('@chainlit/react-client', () => ({
  useChatInteract: () => ({ editMessage, toggleMessageFavorite: vi.fn() }),
  useConfig: () => ({ config: { features: { favorites: false } } }),
  useSetChainlitState: () => setMessages
}));

const message: IStep = {
  threadId: '',
  id: 'message-id',
  name: 'alice',
  type: 'user_message',
  output: 'original',
  createdAt: '2026-11-08T00:00:00.000Z',
  metadata: { location: 'http://localhost:8000/' }
};

const renderMessage = () =>
  render(
    <MessageContext.Provider
      value={{ ...defaultMessageContext, editable: true }}
    >
      <UserMessage message={message} elements={[]}>
        {message.output}
      </UserMessage>
    </MessageContext.Provider>
  );

const startEditing = () => {
  renderMessage();
  fireEvent.click(screen.getByRole('button'));
  return screen.getByRole('textbox');
};

const confirmEdit = () =>
  fireEvent.click(
    screen.getByRole('button', { name: 'common.actions.confirm' })
  );

describe('UserMessage edit frame limit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('edits a message below the Engine.IO 1 MB frame limit', () => {
    const textarea = startEditing();
    fireEvent.change(textarea, { target: { value: 'edited' } });
    confirmEdit();

    expect(editMessage).toHaveBeenCalledWith({ ...message, output: 'edited' });
    expect(setMessages).toHaveBeenCalledTimes(1);
    expect(toastError).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('refuses an oversized edit and keeps the draft', () => {
    const draft = 'a'.repeat(1_200_000);
    const textarea = startEditing();
    fireEvent.change(textarea, { target: { value: draft } });
    confirmEdit();

    expect(editMessage).not.toHaveBeenCalled();
    expect(setMessages).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith(
      'Message is too large. The maximum message size is 1 MB.'
    );
    expect(screen.getByRole('textbox')).toHaveValue(draft);
  });
});
