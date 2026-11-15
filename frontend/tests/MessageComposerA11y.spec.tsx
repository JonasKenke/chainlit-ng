import type { FileSpec } from '@chainlit-ng/react-client';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Attachments } from '@/components/chat/MessageComposer/Attachments';
import SubmitButton from '@/components/chat/MessageComposer/SubmitButton';
import { UploadButton } from '@/components/chat/MessageComposer/UploadButton';

import {
  AppStateProvider,
  type IAttachment,
  createAppStateStore
} from '@/state/store';

const { chatData, stopTask, uploadFeature } = vi.hoisted(() => ({
  chatData: { loading: false, firstInteraction: true },
  stopTask: vi.fn(),
  uploadFeature: { enabled: true }
}));

vi.mock('@/components/i18n/Translator', () => ({
  default: () => null,
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

vi.mock('@chainlit-ng/react-client', () => ({
  useChatData: () => chatData,
  useChatMessages: () => ({ firstInteraction: chatData.firstInteraction }),
  useChatInteract: () => ({ stopTask }),
  useConfig: () => ({
    config: { features: { spontaneous_file_upload: uploadFeature } }
  })
}));

vi.mock('@/hooks/useUpload', () => ({
  useUpload: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({})
  })
}));

const fileSpec = {
  accept: [],
  max_files: 1,
  max_size_mb: 1
} satisfies FileSpec;

const renderUploadButton = (disabled = false) =>
  render(
    <UploadButton
      disabled={disabled}
      fileSpec={fileSpec}
      onFileUpload={vi.fn()}
      onFileUploadError={vi.fn()}
    />
  );

const renderAttachments = (attachments: IAttachment[]) =>
  render(
    <AppStateProvider store={createAppStateStore({ attachments })}>
      <Attachments />
    </AppStateProvider>
  );

beforeEach(() => {
  vi.clearAllMocks();
  chatData.loading = false;
  chatData.firstInteraction = true;
  uploadFeature.enabled = true;
});

describe('MessageComposer icon-only controls', () => {
  it('names the send button and no stop button is exposed while idle', () => {
    const onSubmit = vi.fn();
    render(<SubmitButton onSubmit={onSubmit} />);

    const send = screen.getByRole('button', { name: 'Send message' });
    expect(send).toHaveAttribute('id', 'chat-submit');
    expect(
      screen.queryByRole('button', { name: 'Stop Task' })
    ).not.toBeInTheDocument();

    fireEvent.click(send);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('keeps the send button named when disabled and does not submit', () => {
    const onSubmit = vi.fn();
    render(<SubmitButton disabled onSubmit={onSubmit} />);

    const send = screen.getByRole('button', { name: 'Send message' });
    expect(send).toBeDisabled();

    fireEvent.click(send);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('names the stop button while a task is loading and stops on click', () => {
    chatData.loading = true;
    const onSubmit = vi.fn();

    render(<SubmitButton onSubmit={onSubmit} />);
    const stop = screen.getByRole('button', { name: 'Stop Task' });

    expect(stop).toHaveAttribute('id', 'stop-button');
    fireEvent.click(stop);
    expect(stopTask).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('prefers the send button before the first interaction even when loading', () => {
    chatData.loading = true;
    chatData.firstInteraction = false;

    render(<SubmitButton onSubmit={vi.fn()} />);

    expect(
      screen.getByRole('button', { name: 'Send message' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Stop Task' })
    ).not.toBeInTheDocument();
  });

  it('names the upload button', () => {
    renderUploadButton();

    expect(
      screen.getByRole('button', { name: 'Attach files' })
    ).toHaveAttribute('id', 'upload-button');
  });

  it('keeps the upload button named when disabled', () => {
    renderUploadButton(true);

    const upload = screen.getByRole('button', { name: 'Attach files' });
    expect(upload).toHaveAttribute('id', 'upload-button-loading');
    expect(upload).toBeDisabled();
  });

  it('renders no upload button when spontaneous upload is disabled', () => {
    uploadFeature.enabled = false;

    const { container } = renderUploadButton();

    expect(container).toBeEmptyDOMElement();
  });

  it('names the attachment remove button and removes the attachment', () => {
    const remove = vi.fn();
    renderAttachments([
      {
        id: 'attachment-1',
        name: 'notes.txt',
        size: 10,
        type: 'text/plain',
        uploaded: true,
        remove
      }
    ]);

    const removeButton = screen.getByRole('button', {
      name: 'Remove attachment'
    });
    expect(removeButton.tagName).toBe('BUTTON');
    expect(removeButton).toHaveTextContent('');

    fireEvent.click(removeButton);
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it('renders no remove button when the attachment cannot be removed', () => {
    renderAttachments([
      {
        id: 'attachment-1',
        name: 'notes.txt',
        size: 10,
        type: 'text/plain',
        uploaded: true
      }
    ]);

    expect(
      screen.queryByRole('button', { name: 'Remove attachment' })
    ).not.toBeInTheDocument();
  });

  it('renders nothing when there are no attachments', () => {
    const { container } = renderAttachments([]);

    expect(container).toBeEmptyDOMElement();
  });
});
