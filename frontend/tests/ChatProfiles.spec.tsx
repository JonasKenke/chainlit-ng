import { render, screen } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatProfiles from '@/components/header/ChatProfiles';

import { AppStateProvider } from '@/state/store';

const mocks = vi.hoisted(() => ({
  config: undefined as any,
  setChatProfile: vi.fn(),
  clear: vi.fn()
}));

vi.mock('@chainlit-ng/react-client', async () => {
  const { createContext } = await import('react');

  return {
    ChainlitContext: createContext({
      buildEndpoint: (path: string) => path
    }),
    useConfig: () => ({ config: mocks.config }),
    useChatSession: () => ({
      chatProfile: undefined,
      setChatProfile: mocks.setChatProfile
    }),
    useChatMessages: () => ({ firstInteraction: undefined }),
    useChatInteract: () => ({ clear: mocks.clear })
  };
});

vi.mock('@/components/Markdown', () => ({
  Markdown: ({ children }: PropsWithChildren) => <div>{children}</div>
}));

vi.mock('@/components/header/NewChat', () => ({
  NewChatDialog: () => null
}));

// Radix Select does not open under jsdom pointer events; this test is about
// hook order, so render the options directly.
vi.mock('@/components/ui/select', () => ({
  Select: ({ children }: PropsWithChildren) => <div>{children}</div>,
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: PropsWithChildren) => <div>{children}</div>,
  SelectItem: ({ children, value }: PropsWithChildren<{ value: string }>) => (
    <div data-value={value}>{children}</div>
  )
}));

const wrapper = ({ children }: PropsWithChildren) => (
  <AppStateProvider>{children}</AppStateProvider>
);

describe('ChatProfiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.config = undefined;
  });

  it('renders profiles after an empty profile list without violating hook order', () => {
    mocks.config = { chatProfiles: [], features: {} };
    const { container, rerender } = render(<ChatProfiles />, { wrapper });
    expect(container).toBeEmptyDOMElement();

    mocks.config = {
      chatProfiles: [
        {
          name: 'gpt-4',
          display_name: 'GPT-4',
          markdown_description: 'Most capable'
        },
        {
          name: 'gpt-4o',
          display_name: 'GPT-4o',
          markdown_description: 'Fastest'
        }
      ],
      features: {}
    };

    expect(() => rerender(<ChatProfiles />)).not.toThrow();
    expect(screen.getByText('GPT-4')).toBeInTheDocument();
    expect(screen.getByText('GPT-4o')).toBeInTheDocument();
  });
});
