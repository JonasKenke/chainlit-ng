import { ChainlitContext, type IQuestion } from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageContext } from 'contexts/MessageContext';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { QuestionCard } from '@/components/chat/Questions/QuestionCard';

const respondToQuestion = vi.fn();
const setQuestions = vi.fn();
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      ({
        'chat.question.title': 'Question',
        'chat.question.ariaLabel': 'Question from the assistant',
        'chat.question.options': 'Options',
        'chat.question.other': 'Other',
        'chat.question.otherAriaLabel': 'Other answer',
        'chat.question.answer': 'Your answer',
        'chat.question.answerAriaLabel': 'Your answer',
        'chat.question.readOnly': 'This question can no longer be answered.',
        'chat.question.answeredPrivately': 'Answered privately',
        'chat.question.by': 'By',
        'chat.question.errors.answer': 'Unable to answer question',
        'common.actions.submit': 'Submit'
      })[key] ??
      options?.defaultValue ??
      key
  })
}));

vi.mock('@chainlit-ng/react-client', async () => {
  const ReactModule = await import('react');
  return {
    ChainlitContext: ReactModule.createContext({}),
    useChainlitStore: (selector: (state: any) => unknown) =>
      selector({ sessionId: 'session-1' }),
    useSetChainlitState: () => setQuestions
  };
});

const question: IQuestion = {
  kind: 'question',
  id: 'question-1',
  stepId: 'step-1',
  threadId: 'thread-1',
  status: 'pending',
  prompt: 'Which environment?',
  options: [
    { id: 'staging', label: 'Staging', description: 'Pre-production' },
    { id: 'prod', label: 'Production' }
  ],
  allowFreeText: true,
  live: true
};

function renderCard(overrides: Partial<typeof question> = {}) {
  return render(
    <ChainlitContext.Provider value={{ respondToQuestion } as any}>
      <MessageContext.Provider value={{ cot: 'hidden' } as any}>
        <QuestionCard question={{ ...question, ...overrides }} />
      </MessageContext.Provider>
    </ChainlitContext.Provider>
  );
}

describe('QuestionCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a live card with cot hidden and a disabled submit', () => {
    renderCard();

    expect(screen.getByTestId('question-card')).toBeInTheDocument();
    expect(screen.getByText('Question')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for your answer')).toBeNull();
    expect(screen.getByText('Which environment?')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Staging/ })).toBeInTheDocument();
    expect(screen.getByLabelText('Other answer')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeDisabled();
    expect(screen.queryByTestId('approval-card')).toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Approve' })
    ).not.toBeInTheDocument();
  });

  it('renders every option from the typed option list', () => {
    renderCard();

    for (const { id, label } of question.options) {
      expect(
        screen.getByRole('radio', { name: new RegExp(label) })
      ).toHaveAttribute('value', id);
    }
  });

  it('submits the selected server option id', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { optionId: 'staging', label: 'Staging' },
      revision: 2,
      durable: true
    });
    renderCard();

    fireEvent.click(screen.getByRole('radio', { name: /Staging/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionId: 'session-1',
        questionId: 'question-1',
        optionId: 'staging'
      })
    );
    expect(respondToQuestion.mock.calls[0][0]).not.toHaveProperty('text');
    expect(setQuestions).toHaveBeenCalled();
  });

  it('submits trimmed free text and clears the option selection', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { text: 'blue' },
      revision: 2,
      durable: true
    });
    renderCard();

    fireEvent.click(screen.getByRole('radio', { name: /Staging/ }));
    fireEvent.change(screen.getByLabelText('Other answer'), {
      target: { value: '  blue  ' }
    });
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion).toHaveBeenCalledWith(
      expect.objectContaining({ text: 'blue' })
    );
    expect(respondToQuestion.mock.calls[0][0]).not.toHaveProperty('optionId');
  });

  it('labels free text as an answer when there are no options', () => {
    renderCard({ options: [], allowFreeText: true });

    expect(screen.getByText('Your answer')).toBeInTheDocument();
    expect(screen.getByLabelText('Your answer')).toHaveAttribute(
      'placeholder',
      'Type your answer...'
    );
    expect(screen.queryByText('or')).not.toBeInTheDocument();
  });

  it('shows the free-text placeholder and the option/answer separator', () => {
    renderCard();

    expect(screen.getByLabelText('Other answer')).toHaveAttribute(
      'placeholder',
      'Type your answer...'
    );
    expect(screen.getByText('or')).toBeInTheDocument();
  });

  it('hides the free-text field when the question is closed', () => {
    renderCard({ allowFreeText: false });

    expect(screen.queryByLabelText('Other answer')).toBeNull();
  });

  it('shows the terminal option label and no controls', () => {
    renderCard({
      status: 'answered',
      answer: { optionId: 'prod', label: 'Production' },
      decidedBy: 'alice@example.com',
      decidedAt: '2026-11-08T15:00:00Z'
    });

    expect(screen.queryByText('Answered')).toBeNull();
    expect(screen.getByText('Production')).toBeInTheDocument();
    expect(screen.getByText(/alice@example.com/)).toBeInTheDocument();
    const decidedAt = screen.getByText(/2026/);
    expect(decidedAt.tagName).toBe('TIME');
    expect(decidedAt).toHaveAttribute('dateTime', '2026-11-08T15:00:00Z');
    expect(screen.queryByText('2026-11-08T15:00:00Z')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
  });

  it('shows a redacted free-text answer as private text', () => {
    renderCard({
      status: 'answered',
      answer: { text: null, redacted: true }
    });

    expect(screen.getByText('Answered privately')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('renders no status label for an expired question', () => {
    renderCard({ status: 'expired' });

    expect(screen.queryByText('Expired')).toBeNull();
    expect(
      screen.getByText('This question can no longer be answered.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
  });

  it('draws option and free-text focus rings inside the control', () => {
    renderCard();

    for (const control of [
      screen.getByRole('radio', { name: /Staging/ }),
      screen.getByLabelText('Other answer')
    ]) {
      expect(control.className).toContain('focus-visible:ring-inset');
      expect(control.className).toContain('focus-visible:ring-primary');
      expect(control.className).toContain('focus-visible:ring-2');
    }
  });

  it('does not clip the scrollable body horizontally', () => {
    renderCard();

    const scroll = screen.getByTestId('question-card-scroll');
    expect(scroll).toHaveClass('overflow-y-auto');
    expect(scroll.className).not.toContain('overflow-hidden');
    expect(scroll.className).not.toContain('overflow-x-hidden');
  });

  it('renders a pending card as read-only when it is not live', () => {
    renderCard({ live: false });

    expect(
      screen.getByText('This question can no longer be answered.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
  });

  it('renders prompt and labels as text, never as HTML', () => {
    const prompt = '<img src=x onerror="alert(1)"> Approve';
    const { container } = renderCard({
      prompt,
      options: [{ id: 'a', label: '<b>not bold</b>', description: null }]
    });

    expect(screen.getByText(prompt)).toBeInTheDocument();
    expect(screen.getByText('<b>not bold</b>')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  });

  it('renders Markdown syntax literally', () => {
    const prompt = '**not bold** [not a link](https://example.com)';
    const { container } = renderCard({
      prompt,
      options: [{ id: 'a', label: '`not code`', description: '_not em_' }]
    });

    expect(screen.getByText(prompt)).toBeInTheDocument();
    expect(screen.getByText('`not code`')).toBeInTheDocument();
    expect(screen.getByText('_not em_')).toBeInTheDocument();
    expect(container.querySelector('strong, a, em, code')).toBeNull();
  });

  it('isolates bidi payloads so they cannot reorder the header or labels', () => {
    const prompt = '\u202Egnp.yek \u202C';
    const label = '\u202Eeurt si siht\u202C';
    renderCard({
      prompt,
      options: [{ id: 'a', label, description: null }]
    });

    const header = screen.getByText('Question');
    const promptNode = screen.getByText(prompt);
    const labelNode = screen.getByText(label);

    expect(promptNode.closest('bdi')).toHaveAttribute('dir', 'auto');
    expect(labelNode.closest('bdi')).toHaveAttribute('dir', 'auto');
    expect(
      header.compareDocumentPosition(promptNode) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it('renders zero-width text without creating an approval control', () => {
    const prompt = 'A\u200Bpprove this request';
    renderCard({ prompt });

    expect(screen.getByText(prompt)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Approve/i })
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-card')).toBeNull();
  });

  it('pins the Submit action outside the scrollable body and keeps it focusable', () => {
    renderCard();

    const card = screen.getByTestId('question-card');
    const scroll = screen.getByTestId('question-card-scroll');
    const actions = screen.getByTestId('question-card-actions');
    const submit = screen.getByRole('button', { name: 'Submit' });

    expect(card).toContainElement(scroll);
    expect(card).toContainElement(actions);
    expect(card).toContainElement(submit);
    expect(scroll).toContainElement(screen.getByText('Which environment?'));
    expect(scroll).toContainElement(
      screen.getByRole('radio', { name: /Staging/ })
    );
    expect(scroll).toContainElement(screen.getByLabelText('Other answer'));
    expect(scroll).not.toContainElement(submit);
    expect(actions).toContainElement(submit);
    expect(actions).not.toHaveAttribute('aria-hidden');
    expect(card).not.toHaveAttribute('inert');

    fireEvent.click(screen.getByRole('radio', { name: /Staging/ }));
    expect(submit).toBeEnabled();
    submit.focus();
    expect(submit).toHaveFocus();
    expect(submit.tabIndex).toBe(0);
  });

  it('enables Submit for either answer source and disables it when empty', () => {
    renderCard();

    const submit = screen.getByRole('button', { name: 'Submit' });
    const other = screen.getByLabelText('Other answer');

    expect(submit).toBeDisabled();
    fireEvent.change(other, { target: { value: '   ' } });
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByRole('radio', { name: /Staging/ }));
    expect(submit).toBeEnabled();

    fireEvent.change(other, { target: { value: 'custom answer' } });
    expect(screen.getByRole('radio', { name: /Staging/ })).not.toBeChecked();
    expect(submit).toBeEnabled();

    fireEvent.change(other, { target: { value: '' } });
    expect(submit).toBeDisabled();
  });

  it('keeps Submit in the DOM but disabled while an answer is in flight', async () => {
    const { promise, resolve: resolveAnswer } = Promise.withResolvers<{
      questionId: string;
      status: 'answered';
      answer: { optionId: string; label: string };
      revision: number;
      durable: boolean;
    }>();
    respondToQuestion.mockReturnValue(promise);
    renderCard();

    fireEvent.click(screen.getByRole('radio', { name: /Staging/ }));
    const submit = screen.getByRole('button', { name: 'Submit' });
    fireEvent.click(submit);

    await waitFor(() => expect(submit).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Submit' })).toBe(submit);
    fireEvent.click(submit);
    expect(respondToQuestion).toHaveBeenCalledTimes(1);

    resolveAnswer({
      questionId: 'question-1',
      status: 'answered',
      answer: { optionId: 'staging', label: 'Staging' },
      revision: 2,
      durable: true
    });
    await waitFor(() => expect(submit).toBeEnabled());
  });
});
