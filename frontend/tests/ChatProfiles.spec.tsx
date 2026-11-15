import { fireEvent, render, screen } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatProfiles from '@/components/header/ChatProfiles';

import { AppStateProvider } from '@/state/store';

const mocks = vi.hoisted(() => ({
  config: undefined as unknown,
  setChatProfile: vi.fn(),
  clear: vi.fn(),
  navigate: vi.fn(),
  pendingState: {
    approvals: [] as { status: string; live?: boolean }[],
    questions: [] as { status: string; live?: boolean }[]
  }
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
    useChatInteract: () => ({ clear: mocks.clear }),
    useChainlitStore: (
      selector: (state: typeof mocks.pendingState) => unknown
    ) => selector(mocks.pendingState)
  };
});

vi.mock('@/components/Markdown', () => ({
  Markdown: ({ children }: PropsWithChildren) => <div>{children}</div>
}));

vi.mock('@/components/header/NewChat', () => ({
  NewChatDialog: () => null
}));

// Radix Select does not open under jsdom pointer events, so render the options
// as buttons that invoke the select's onValueChange directly.
vi.mock('@/components/ui/select', async () => {
  const { createContext, useContext } = await import('react');
  const SelectCallbackContext = createContext<(value: string) => void>(
    () => {}
  );

  return {
    Select: ({
      children,
      onValueChange
    }: {
      children: React.ReactNode;
      onValueChange?: (value: string) => void;
    }) => (
      <SelectCallbackContext.Provider value={onValueChange ?? (() => {})}>
        {children}
      </SelectCallbackContext.Provider>
    ),
    SelectTrigger: () => null,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: React.ReactNode }) => (
      <div>{children}</div>
    ),
    SelectItem: ({
      children,
      value
    }: {
      children: React.ReactNode;
      value: string;
    }) => {
      const onValueChange = useContext(SelectCallbackContext);
      return (
        <button
          type="button"
          data-value={value}
          onClick={() => onValueChange(value)}
        >
          {children}
        </button>
      );
    }
  };
});

const wrapper = ({ children }: PropsWithChildren) => (
  <AppStateProvider>{children}</AppStateProvider>
);

const profiles = [
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
];

describe('ChatProfiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.config = undefined;
    mocks.pendingState.approvals = [];
    mocks.pendingState.questions = [];
  });

  it('renders profiles after an empty profile list without violating hook order', () => {
    mocks.config = { chatProfiles: [], features: {} };
    const { container, rerender } = render(<ChatProfiles />, { wrapper });
    expect(container).toBeEmptyDOMElement();

    mocks.config = { chatProfiles: profiles, features: {} };

    expect(() => rerender(<ChatProfiles />)).not.toThrow();
    expect(screen.getByText('GPT-4')).toBeInTheDocument();
    expect(screen.getByText('GPT-4o')).toBeInTheDocument();
  });

  it('switches profile directly when no interaction is pending', () => {
    mocks.config = { chatProfiles: profiles, features: {} };
    render(<ChatProfiles navigate={mocks.navigate} />, { wrapper });
    mocks.setChatProfile.mockClear();

    fireEvent.click(screen.getByText('GPT-4o'));

    expect(mocks.setChatProfile).toHaveBeenCalledWith('gpt-4o');
    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.navigate).toHaveBeenCalledWith('/');
  });

  it.each([
    ['approval', { approvals: [{ status: 'pending', live: true }] }],
    ['question', { questions: [{ status: 'pending', live: true }] }]
  ])('warns before switching profile with a pending %s', (_kind, pending) => {
    mocks.config = { chatProfiles: profiles, features: {} };
    mocks.pendingState = { ...mocks.pendingState, ...pending };
    render(<ChatProfiles navigate={mocks.navigate} />, { wrapper });
    mocks.setChatProfile.mockClear();

    fireEvent.click(screen.getByText('GPT-4o'));

    expect(
      screen.getByText('Pending approval or question')
    ).toBeInTheDocument();
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.setChatProfile).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it('keeps the current profile and chat when cancelling the warning', () => {
    mocks.config = { chatProfiles: profiles, features: {} };
    mocks.pendingState.approvals = [{ status: 'pending', live: true }];
    render(<ChatProfiles navigate={mocks.navigate} />, { wrapper });
    mocks.setChatProfile.mockClear();

    fireEvent.click(screen.getByText('GPT-4o'));
    fireEvent.click(screen.getByText('Cancel'));

    expect(mocks.setChatProfile).not.toHaveBeenCalled();
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });

  it('switches profile after confirming the warning', () => {
    mocks.config = { chatProfiles: profiles, features: {} };
    mocks.pendingState.questions = [{ status: 'pending', live: true }];
    render(<ChatProfiles navigate={mocks.navigate} />, { wrapper });
    mocks.setChatProfile.mockClear();

    fireEvent.click(screen.getByText('GPT-4o'));
    fireEvent.click(screen.getByText('Switch chat'));

    expect(mocks.setChatProfile).toHaveBeenCalledWith('gpt-4o');
    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.navigate).toHaveBeenCalledWith('/');
  });

  it('does not warn for a hydrated read-only question', () => {
    mocks.config = { chatProfiles: profiles, features: {} };
    mocks.pendingState.questions = [{ status: 'pending', live: false }];
    render(<ChatProfiles navigate={mocks.navigate} />, { wrapper });

    fireEvent.click(screen.getByText('GPT-4o'));

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(mocks.clear).toHaveBeenCalledTimes(1);
  });
});
