import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import NewChatButton from '@/components/header/NewChat';

const { mockClear, pendingState } = vi.hoisted(() => ({
  mockClear: vi.fn(),
  pendingState: {
    approvals: [] as { status: string; live?: boolean }[],
    questions: [] as { status: string; live?: boolean }[]
  }
}));
const mockUseConfig = vi.fn();

vi.mock('@chainlit/react-client', () => ({
  useChatInteract: () => ({ clear: mockClear }),
  useChainlitStore: (selector: (state: typeof pendingState) => unknown) =>
    selector(pendingState),
  useConfig: () => mockUseConfig()
}));

vi.mock('@/components/i18n', () => ({
  Translator: ({ path }: { path: string }) => <span>{path}</span>,
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

describe('NewChatButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    pendingState.approvals = [];
    pendingState.questions = [];
    mockUseConfig.mockReturnValue({ config: {} });
  });

  it('renders the button correctly', () => {
    render(<NewChatButton />);
    const button = screen.getByRole('button');
    expect(button).toBeInTheDocument();
    expect(button.querySelector('svg')).toBeInTheDocument();
  });

  it('opens dialog by default when config is undefined', () => {
    mockUseConfig.mockReturnValue({ config: undefined });

    render(<NewChatButton />);
    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.getByText('navigation.newChat.dialog.title')
    ).toBeInTheDocument();
  });

  it('opens dialog by default when ui config is missing', () => {
    mockUseConfig.mockReturnValue({ config: { project: {} } });

    render(<NewChatButton />);
    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.getByText('navigation.newChat.dialog.title')
    ).toBeInTheDocument();
  });

  it('clears chat and navigates when confirmed via Dialog', () => {
    mockUseConfig.mockReturnValue({ config: {} });
    const mockNavigate = vi.fn();

    render(<NewChatButton navigate={mockNavigate} />);

    fireEvent.click(screen.getByRole('button'));

    const confirmBtn = screen.getByText('common.actions.confirm');
    fireEvent.click(confirmBtn);

    expect(mockClear).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('warns instead of clearing when an approval is pending', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: false } }
    });
    pendingState.approvals = [{ status: 'pending', live: true }];

    render(<NewChatButton />);
    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.getByText('Pending approval or question')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'This chat still has an approval or question waiting. Starting a new chat will discard it.'
      )
    ).toBeInTheDocument();
    expect(mockClear).not.toHaveBeenCalled();
  });

  it('clears after confirming the pending warning', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: false } }
    });
    pendingState.questions = [{ status: 'pending', live: true }];
    const mockNavigate = vi.fn();

    render(<NewChatButton navigate={mockNavigate} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByText('Start new chat'));

    expect(mockClear).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('does not warn for a hydrated read-only question', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: false } }
    });
    pendingState.questions = [{ status: 'pending', live: false }];

    render(<NewChatButton />);
    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(mockClear).toHaveBeenCalledTimes(1);
  });

  it('skips dialog and activates immediately when confirm_new_chat is false', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: false } }
    });

    const mockNavigate = vi.fn();
    render(<NewChatButton navigate={mockNavigate} />);

    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.queryByText('navigation.newChat.dialog.title')
    ).not.toBeInTheDocument();
    expect(mockClear).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('opens dialog explicitly when confirm_new_chat is true', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: true } }
    });

    render(<NewChatButton />);
    fireEvent.click(screen.getByRole('button'));

    expect(
      screen.getByText('navigation.newChat.dialog.title')
    ).toBeInTheDocument();
    expect(mockClear).not.toHaveBeenCalled();
  });

  it('uses custom onConfirm handler if provided', () => {
    mockUseConfig.mockReturnValue({
      config: { ui: { confirm_new_chat: false } }
    });

    const customOnConfirm = vi.fn();
    render(<NewChatButton onConfirm={customOnConfirm} />);

    fireEvent.click(screen.getByRole('button'));

    expect(customOnConfirm).toHaveBeenCalledTimes(1);
    expect(mockClear).not.toHaveBeenCalled();
  });
});
