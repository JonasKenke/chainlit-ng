import type { IStep } from '@chainlit-ng/react-client';
import { useConfig } from '@chainlit-ng/react-client';
import { render } from '@testing-library/react';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Message } from '@/components/chat/Messages/Message';

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({ t: (key: string) => key })
}));

vi.mock('@chainlit-ng/react-client', async () => {
  const { createContext } = await import('react');
  return {
    ChainlitContext: createContext(undefined),
    useConfig: vi.fn(),
    useChatSession: () => ({}),
    useChatInteract: () => ({
      editMessage: vi.fn(),
      toggleMessageFavorite: vi.fn()
    }),
    useSetChainlitState: () => vi.fn(),
    usePromptGallery: () => ({ savePrompt: vi.fn() }),
    useChainlitStore: () => undefined
  };
});

const config = (show_message_timestamps: boolean) =>
  ({
    config: {
      chatProfiles: [],
      ui: { show_message_timestamps },
      features: {}
    }
  }) as ReturnType<typeof useConfig>;

const message = (
  type: IStep['type'],
  createdAt: IStep['createdAt']
): IStep => ({
  id: `step-${type}`,
  name: 'Assistant',
  type,
  output: 'Hello',
  createdAt
});

const renderMessage = (step: IStep, cot: 'hidden' | 'full' = 'hidden') =>
  render(
    <MessageContext.Provider value={{ ...defaultMessageContext, cot }}>
      <Message message={step} elements={[]} actions={[]} indent={0} />
    </MessageContext.Provider>
  );

describe('message timestamps', () => {
  beforeEach(() => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue(config(false));
  });

  it('renders nothing when the setting is off', () => {
    const { container } = renderMessage(
      message('assistant_message', '2024-01-02T03:04:05.000Z')
    );

    expect(container.querySelector('time')).toBeNull();
  });

  it('renders a tooltip and machine-readable date on assistant messages', () => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue(config(true));
    const createdAt = '2024-01-02T03:04:05.000Z';
    const { container } = renderMessage(
      message('assistant_message', createdAt)
    );

    const time = container.querySelector('time');
    expect(time).not.toBeNull();
    expect(time?.getAttribute('dateTime')).toBe(
      new Date(createdAt).toISOString()
    );
    expect(time?.getAttribute('title')).toContain('2024');
    expect(time?.textContent).not.toBe('');
  });

  it('renders on user messages with a numeric createdAt', () => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue(config(true));
    const createdAt = new Date('2024-05-06T07:08:09.000Z').getTime();
    const { container } = renderMessage(message('user_message', createdAt));

    expect(container.querySelector('time')?.getAttribute('dateTime')).toBe(
      new Date(createdAt).toISOString()
    );
  });

  it('does not render on system messages or steps', () => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue(config(true));

    const system = renderMessage(
      message('system_message', '2024-01-02T03:04:05.000Z')
    );
    expect(system.container.querySelector('time')).toBeNull();

    const step = renderMessage(
      { ...message('tool', '2024-01-02T03:04:05.000Z'), output: '' },
      'full'
    );
    expect(step.container.querySelector('time')).toBeNull();
  });

  it('renders nothing for a missing or invalid createdAt', () => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue(config(true));

    const { createdAt: _createdAt, ...withoutCreatedAt } = message(
      'assistant_message',
      '2024-01-02T03:04:05.000Z'
    );
    const missing = renderMessage(withoutCreatedAt as IStep);
    expect(missing.container.querySelector('time')).toBeNull();

    const invalid = renderMessage(message('assistant_message', 'not-a-date'));
    expect(invalid.container.querySelector('time')).toBeNull();
  });
});
