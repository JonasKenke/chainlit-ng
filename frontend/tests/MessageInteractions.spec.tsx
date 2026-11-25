import type { IApproval, IQuestion, IStep } from '@chainlit-ng/react-client';
import { render, screen } from '@testing-library/react';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Message } from '@/components/chat/Messages/Message';
import { StepInteractions } from '@/components/chat/Messages/StepInteractions';

const state = vi.hoisted(() => ({
  approvals: [] as IApproval[],
  questions: [] as IQuestion[]
}));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      ({ 'common.actions.submit': 'Submit' })[key] ??
      options?.defaultValue ??
      key
  })
}));

vi.mock('@/components/chat/Messages', () => ({
  Messages: () => <div data-testid="children-marker" />
}));

vi.mock('@/components/chat/Messages/Message/Content', () => ({
  MessageContent: () => <div data-testid="content-marker" />
}));

vi.mock('@/components/chat/Messages/Message/Step', () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="step-marker">{children}</div>
  )
}));

vi.mock('@/components/chat/Messages/Message/Avatar', () => ({
  MessageAvatar: () => null
}));

vi.mock('@/components/chat/Messages/Message/Buttons', () => ({
  MessageButtons: () => null
}));

vi.mock('@/components/chat/Messages/Message/UserMessage', () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  )
}));

vi.mock('@/components/chat/Messages/Message/Timestamp', () => ({
  MessageTimestamp: () => null
}));

vi.mock('@/components/chat/Messages/Message/AskFileButton', () => ({
  AskFileButton: () => null
}));

vi.mock('@/components/chat/Messages/Message/AskActionButtons', () => ({
  AskActionButtons: () => null
}));

vi.mock('@/hooks/useLayoutMaxWidth', () => ({
  useLayoutMaxWidth: () => 800
}));

vi.mock('@chainlit-ng/react-client', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@chainlit-ng/react-client')>();
  return {
    ...original,
    useChainlitStore: (
      selector: (state: {
        sessionId: string;
        approvals: IApproval[];
        questions: IQuestion[];
      }) => unknown
    ) =>
      selector({
        sessionId: 'session-1',
        approvals: state.approvals,
        questions: state.questions
      })
  };
});

const runStep: IStep = {
  id: 'run-1',
  name: 'on_message',
  type: 'run' as const,
  threadId: 'thread-1',
  output: '',
  createdAt: '2026-11-09T00:00:00Z',
  steps: [
    {
      id: 'answer-1',
      name: 'assistant',
      type: 'assistant_message' as const,
      output: 'done',
      createdAt: '2026-11-09T00:00:01Z'
    }
  ]
};

const pendingApproval = {
  id: 'approval-1',
  stepId: runStep.id,
  threadId: 'thread-1',
  tool: 'send_email',
  title: 'Send the draft',
  status: 'pending' as const,
  arguments: { to: 'person@example.com' }
};

const liveQuestion = {
  kind: 'question' as const,
  id: 'question-1',
  stepId: runStep.id,
  threadId: 'thread-1',
  status: 'answered' as const,
  prompt: 'What should I name the release?',
  options: [],
  allowFreeText: true,
  answer: { text: 'Kestrel' },
  decidedAt: '2026-11-09T00:00:02Z',
  terminalAt: '2026-11-09T00:00:02Z',
  live: true
};

function renderStep() {
  return render(
    <MessageContext.Provider value={{ ...defaultMessageContext, cot: 'full' }}>
      <Message message={runStep} elements={[]} actions={[]} indent={0} />
    </MessageContext.Provider>
  );
}

function stepElement(container: HTMLElement) {
  return container.querySelector('[data-step-type="run"]') as HTMLElement;
}

describe('inline interaction cards', () => {
  beforeEach(() => {
    state.approvals = [];
    state.questions = [];
  });

  it('renders an approval card between the step and its child messages', () => {
    state.approvals = [pendingApproval];

    const { container } = renderStep();

    const step = stepElement(container);
    const card = screen.getByTestId('approval-card');
    const children = screen
      .getAllByTestId('children-marker')
      .find(
        (marker) =>
          card.compareDocumentPosition(marker) &
          Node.DOCUMENT_POSITION_FOLLOWING
      );
    expect(
      step.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(children).toBeDefined();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('groups concurrent pending approvals into one queue card', () => {
    state.approvals = [
      { ...pendingApproval, id: 'approval-1', tool: 'send_email' },
      { ...pendingApproval, id: 'approval-2', tool: 'delete_file' }
    ];

    renderStep();

    expect(screen.getByTestId('approval-queue-card')).toBeInTheDocument();
    expect(screen.getAllByTestId('approval-card')).toHaveLength(2);
  });

  it('renders one queue across concurrent tool steps', () => {
    state.approvals = [
      { ...pendingApproval, id: 'approval-1', stepId: 'tool-1' },
      { ...pendingApproval, id: 'approval-2', stepId: 'tool-2' }
    ];

    render(
      <MessageContext.Provider
        value={{ ...defaultMessageContext, cot: 'full' }}
      >
        <StepInteractions stepId="tool-1" threadId="thread-1" />
        <StepInteractions stepId="tool-2" threadId="thread-1" />
      </MessageContext.Provider>
    );

    expect(screen.getAllByTestId('approval-queue-card')).toHaveLength(1);
    expect(screen.getAllByTestId('approval-card')).toHaveLength(2);
  });

  it('renders a question card between the step and its child messages', () => {
    state.questions = [liveQuestion];

    const { container } = renderStep();

    const step = stepElement(container);
    const card = screen.getByTestId('question-card');
    const children = screen
      .getAllByTestId('children-marker')
      .find(
        (marker) =>
          card.compareDocumentPosition(marker) &
          Node.DOCUMENT_POSITION_FOLLOWING
      );
    expect(
      step.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(children).toBeDefined();
  });

  it('keeps the question card answerable while it is live', () => {
    state.questions = [{ ...liveQuestion, status: 'pending', answer: null }];

    renderStep();

    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument();
  });

  it('renders a hydrated question from the resumed thread read-only', () => {
    state.questions = [
      { ...liveQuestion, status: 'pending', answer: null, live: false }
    ];

    renderStep();

    expect(screen.getByTestId('question-card')).toBeInTheDocument();
    expect(
      screen.getByText('chat.question.status.orphaned')
    ).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
  });

  it('ignores a hydrated question from another thread', () => {
    state.questions = [{ ...liveQuestion, threadId: 'thread-2', live: false }];

    renderStep();

    expect(screen.queryByTestId('question-card')).toBeNull();
  });
});
