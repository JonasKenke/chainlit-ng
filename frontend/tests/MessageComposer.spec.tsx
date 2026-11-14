import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MessageComposer from '@/components/chat/MessageComposer';

import { AppStateProvider, createAppStateStore } from '@/state/store';

const { sendMessage, toastError, toastWarning, chainlitStore, chatData } =
  vi.hoisted(() => ({
    sendMessage: vi.fn(),
    toastError: vi.fn(),
    toastWarning: vi.fn(),
    chatData: {
      askUser: undefined,
      chatSettingsInputs: [],
      disabled: false
    },
    chainlitStore: {
      commands: [] as unknown[],
      approvals: [] as unknown[],
      questions: [] as unknown[]
    }
  }));

vi.mock('sonner', () => ({
  toast: { error: toastError, warning: toastWarning }
}));

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
  useChatData: () => chatData,
  useChatInteract: () => ({
    sendMessage,
    replyMessage: vi.fn(),
    updateMode: vi.fn()
  }),
  useChatMessages: () => ({ firstInteraction: false }),
  useConfig: () => ({ config: { ui: {} } }),
  useChainlitState: () => [[], vi.fn()],
  useChainlitStore: (
    selector: (state: {
      commands: unknown[];
      approvals: unknown[];
      questions: unknown[];
    }) => unknown
  ) => selector(chainlitStore)
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
vi.mock('@/components/chat/MessageComposer/Mcp/Library', () => ({
  McpLibraryButton: () => null
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

const resetMocks = () => {
  vi.clearAllMocks();
  chatData.disabled = false;
  chainlitStore.commands = [];
  chainlitStore.approvals = [];
  chainlitStore.questions = [];
};

describe('MessageComposer pending interaction', () => {
  beforeEach(resetMocks);

  it('does not send a pending live approval and warns with the draft kept', () => {
    chatData.disabled = true;
    chainlitStore.approvals = [{ id: 'a1', status: 'pending', live: true }];
    renderComposer();

    expect(screen.getByTestId('pending-interaction-hint')).toHaveTextContent(
      'chat.input.errors.pendingInteraction'
    );

    setDraft('hello');
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });

    expect(sendMessage).not.toHaveBeenCalled();
    expect(toastWarning).toHaveBeenCalledWith(
      'chat.input.errors.pendingInteraction'
    );
    expect(screen.getByRole('textbox')).toHaveValue('hello');
  });

  it('blocks sending while an approval is pending even when loading ended', () => {
    chainlitStore.approvals = [{ id: 'a1', status: 'pending' }];
    renderComposer();

    setDraft('hello');
    fireEvent.click(screen.getByRole('button'));

    expect(sendMessage).not.toHaveBeenCalled();
    expect(toastWarning).toHaveBeenCalledWith(
      'chat.input.errors.pendingInteraction'
    );
    expect(screen.getByRole('textbox')).toHaveValue('hello');
  });

  it('shows the hint for a pending live question', () => {
    chainlitStore.questions = [{ id: 'q1', status: 'pending', live: true }];
    renderComposer();

    expect(screen.getByTestId('pending-interaction-hint')).toBeInTheDocument();
  });

  it('does not show the hint for a hydrated question', () => {
    chainlitStore.questions = [{ id: 'q1', status: 'pending', live: false }];
    renderComposer();

    expect(screen.queryByTestId('pending-interaction-hint')).toBeNull();
  });

  it('does not show the hint for terminal approvals', () => {
    chainlitStore.approvals = [
      { id: 'a1', status: 'approved', live: true },
      { id: 'a2', status: 'expired', live: true }
    ];
    renderComposer();

    expect(screen.queryByTestId('pending-interaction-hint')).toBeNull();
  });

  it('sends without warning when no interaction is pending', () => {
    chainlitStore.approvals = [{ id: 'a1', status: 'rejected', live: true }];
    renderComposer();

    setDraft('hello');
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });

    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ output: 'hello' }),
      []
    );
    expect(toastWarning).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox')).toHaveValue('');
  });
});

describe('MessageComposer frame limit', () => {
  beforeEach(resetMocks);

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
