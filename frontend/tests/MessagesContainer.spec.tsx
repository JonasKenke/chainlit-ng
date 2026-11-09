import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MessagesContainer from '@/components/chat/MessagesContainer';

const state = vi.hoisted(() => ({
  questions: [] as any[],
  threadId: 'thread-1'
}));

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

vi.mock('@/components/chat/Messages', () => ({ Messages: () => null }));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        'chat.question.title': 'Question',
        'chat.question.ariaLabel': 'Question from the assistant',
        'chat.question.options': 'Options',
        'chat.question.other': 'Other',
        'chat.question.otherAriaLabel': 'Other answer',
        'chat.question.readOnly': 'This question can no longer be answered.',
        'chat.question.answeredPrivately': 'Answered privately',
        'chat.question.by': 'By',
        'chat.question.status.pending': 'Waiting for your answer',
        'chat.question.status.answered': 'Answered',
        'chat.question.status.expired': 'Expired',
        'chat.question.status.superseded': 'Superseded',
        'chat.question.status.orphaned': 'Unavailable',
        'chat.question.errors.answer': 'Unable to answer question',
        'common.actions.submit': 'Submit'
      })[key] ?? key
  })
}));

vi.mock('@chainlit/react-client', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@chainlit/react-client')>();
  return {
    ...original,
    useConfig: () => ({ config: { features: {}, ui: {} } }),
    useChatData: () => ({
      elements: [],
      askUser: vi.fn(),
      loading: false,
      actions: []
    }),
    useChatMessages: () => ({ threadId: state.threadId, messages: [] }),
    useChatInteract: () => ({ uploadFile: vi.fn() }),
    useChainlitStore: (selector: (state: any) => unknown) =>
      selector({
        sessionId: 'session-1',
        approvals: [],
        questions: state.questions
      }),
    useSetChainlitState: () => vi.fn()
  };
});

const answeredQuestion = {
  kind: 'question' as const,
  id: 'question-1',
  stepId: 'step-1',
  threadId: 'thread-1',
  status: 'answered' as const,
  prompt: 'What should I name the release?',
  options: [],
  allowFreeText: true,
  answer: { text: 'Kestrel' },
  decidedAt: '2026-11-09T00:00:00Z',
  terminalAt: '2026-11-09T00:00:00Z',
  live: false
};

describe('MessagesContainer question cards', () => {
  beforeEach(() => {
    state.questions = [];
    state.threadId = 'thread-1';
  });

  it('renders a hydrated question from the resumed thread read-only', () => {
    state.questions = [answeredQuestion];

    render(<MessagesContainer />);

    expect(screen.getByTestId('question-card')).toBeInTheDocument();
    expect(
      screen.getByText('What should I name the release?')
    ).toBeInTheDocument();
    expect(screen.getByText('Kestrel')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
  });

  it('renders a hydrated pending question as read-only instead of re-arming it', () => {
    state.questions = [
      {
        ...answeredQuestion,
        status: 'pending',
        answer: null,
        decidedAt: null,
        terminalAt: null
      }
    ];

    render(<MessagesContainer />);

    expect(
      screen.getByText('This question can no longer be answered.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
  });

  it('keeps a live question answerable', () => {
    state.questions = [
      {
        ...answeredQuestion,
        status: 'pending',
        answer: null,
        decidedAt: null,
        terminalAt: null,
        live: true
      }
    ];

    render(<MessagesContainer />);

    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument();
  });

  it('ignores a hydrated question from another thread', () => {
    state.threadId = 'thread-2';
    state.questions = [answeredQuestion];

    render(<MessagesContainer />);

    expect(screen.queryByTestId('question-card')).toBeNull();
  });
});
