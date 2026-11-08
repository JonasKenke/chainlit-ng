import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MessageComposer from '@/components/chat/MessageComposer';

import { AppStateProvider, createAppStateStore } from '@/state/store';

const { sendMessage, toastError } = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  toastError: vi.fn()
}));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({ t: (key: string) => key })
}));

vi.mock('@/hooks/query', () => ({
  useQuery: () => new URLSearchParams()
}));

vi.mock('@/hooks/use-mobile', () => ({
  useIsMobile: () => false
}));

vi.mock('@chainlit/react-client', () => ({
  useAuth: () => ({ user: { identifier: 'alice' } }),
  useChatData: () => ({
    askUser: undefined,
    chatSettingsInputs: [],
    disabled: false
  }),
  useChatInteract: () => ({
    sendMessage,
    replyMessage: vi.fn(),
    updateMode: vi.fn()
  }),
  useChatMessages: () => ({ firstInteraction: false }),
  useConfig: () => ({ config: { ui: {} } }),
  useChainlitState: () => [[], vi.fn()],
  useChainlitStore: (selector: (state: { commands: [] }) => unknown) =>
    selector({ commands: [] })
}));

vi.mock('@/components/chat/MessageComposer/Attachments', () => ({
  Attachments: () => null
}));
vi.mock('@/components/chat/MessageComposer/CommandButtons', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/CommandPopoverButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/FavoriteButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/Mcp', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/ModePicker', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/PromptGalleryButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/PromptSchedulingButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/UploadButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/VoiceButton', () => ({
  default: () => null
}));
vi.mock('@/components/chat/MessageComposer/SubmitButton', () => ({
  default: ({
    disabled,
    onSubmit
  }: {
    disabled?: boolean;
    onSubmit: () => void;
  }) => <button id="chat-submit" disabled={disabled} onClick={onSubmit} />
}));

const renderComposer = () =>
  render(
    <AppStateProvider store={createAppStateStore()}>
      <MessageComposer
        fileSpec={{ accept: [], max_files: 1, max_size_mb: 1 } as any}
        onFileUpload={vi.fn()}
        onFileUploadError={vi.fn()}
        autoScrollRef={{ current: false }}
      />
    </AppStateProvider>
  );

const setDraft = (value: string) =>
  fireEvent.change(screen.getByRole('textbox'), { target: { value } });

describe('MessageComposer frame limit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sends a message below the Engine.IO 1 MB frame limit', () => {
    renderComposer();
    setDraft('hello');
    fireEvent.click(screen.getByRole('button'));

    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ output: 'hello' }),
      []
    );
    expect(toastError).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('shows an error and keeps the draft when the frame exceeds 1 MB', () => {
    renderComposer();
    setDraft('a'.repeat(1_200_000));
    fireEvent.click(screen.getByRole('button'));

    expect(sendMessage).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith('chat.input.errors.tooLarge');
    expect(
      (screen.getByRole('textbox') as HTMLTextAreaElement).value.length
    ).toBe(1_200_000);
  });
});
