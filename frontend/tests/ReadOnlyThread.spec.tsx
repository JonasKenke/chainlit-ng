import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { ReadOnlyThread } from '@/components/ReadOnlyThread';

const thread = {
  id: 'thread-1',
  metadata: { is_shared: true },
  steps: [
    {
      id: 'step-1',
      name: 'question-step',
      type: 'tool',
      metadata: {
        approval: {
          kind: 'question',
          id: 'question-1',
          stepId: 'step-1',
          threadId: 'thread-1',
          status: 'answered',
          prompt: 'What should I name the release?',
          options: [],
          allowFreeText: true,
          answer: { text: null, redacted: true },
          decidedAt: '2026-11-09T00:00:00Z',
          terminalAt: '2026-11-09T00:00:00Z'
        }
      }
    }
  ],
  elements: []
};

vi.mock('@chainlit/react-client', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@chainlit/react-client')>();
  return {
    ...original,
    useApi: () => ({ data: thread, error: null, isLoading: false }),
    useConfig: () => ({ config: { features: {}, ui: {} } }),
    useChainlitStore: (selector: (state: any) => unknown) =>
      selector({ sessionId: 'session-1' }),
    useSetChainlitState: () => vi.fn(),
    nestMessages: () => []
  };
});

vi.mock('@/components/chat/Messages', () => ({
  Messages: () => null
}));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        'chat.question.title': 'Question',
        'chat.question.ariaLabel': 'Question from the assistant',
        'chat.question.readOnly': 'This question can no longer be answered.',
        'chat.question.answeredPrivately': 'Answered privately',
        'chat.question.by': 'By',
        'chat.question.status.answered': 'Answered',
        'common.actions.submit': 'Submit'
      })[key] ?? key
  })
}));

describe('ReadOnlyThread question cards', () => {
  it('renders a persisted question after the messages, read-only', () => {
    render(
      <MemoryRouter initialEntries={['/share/thread-1']}>
        <ReadOnlyThread id="thread-1" />
      </MemoryRouter>
    );

    expect(screen.getByTestId('question-card')).toBeInTheDocument();
    expect(
      screen.getByText('What should I name the release?')
    ).toBeInTheDocument();
    expect(screen.getByText('Answered privately')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});
