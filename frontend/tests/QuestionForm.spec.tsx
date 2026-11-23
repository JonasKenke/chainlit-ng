import {
  type ChainlitAPI,
  ChainlitContext,
  type IQuestion,
  type IQuestionField
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageContext, defaultMessageContext } from 'contexts/MessageContext';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { QuestionCard } from '@/components/chat/Questions/QuestionCard';

const respondToQuestion = vi.fn();
const setQuestions = vi.fn();
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    i18n: { language: 'en-US' },
    t: (key: string, options?: Record<string, unknown>) => {
      if (key === 'chat.question.progress') {
        return `${options?.current}/${options?.total}`;
      }
      return (
        {
          'chat.question.title': 'Question',
          'chat.question.ariaLabel': 'Question from the assistant',
          'chat.question.progress': '1/3',
          'chat.question.next': 'Next',
          'chat.question.back': 'Back',
          'chat.question.reviewTitle': 'Review your answers',
          'chat.question.edit': 'Edit',
          'chat.question.optional': 'Optional',
          'chat.question.selectMultiple': 'Select all that apply',
          'chat.question.otherAriaLabel': 'Other answer',
          'chat.question.and': 'and',
          'chat.question.or': 'or',
          'chat.question.notAnswered': 'Not answered',
          'chat.question.datePlaceholder': 'Pick a date',
          'chat.question.clear': 'Clear',
          'chat.question.answeredPrivately': 'Answered privately',
          'chat.question.errors.required': 'This question is required',
          'chat.question.errors.email': 'Enter a valid email address',
          'chat.question.errors.date': 'Choose a valid date',
          'chat.question.errors.number': 'Enter a valid number',
          'chat.question.errors.url': 'Enter a valid URL',
          'chat.question.errors.tel': 'Enter a valid phone number',
          'chat.question.errors.answer': 'Unable to answer question',
          'common.actions.submit': 'Submit'
        }[key] ??
        (options?.defaultValue as string | undefined) ??
        key
      );
    }
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
  prompt: 'Your details',
  options: [],
  allowFreeText: false,
  structured: true,
  multi: true,
  live: true,
  questions: [
    {
      id: 'name',
      prompt: 'Your name',
      type: 'text',
      options: [],
      allowFreeText: true,
      default: 'Ada',
      required: true,
      multiline: false
    },
    {
      id: 'email',
      prompt: 'Email',
      type: 'email',
      options: [],
      allowFreeText: true,
      required: true
    },
    {
      id: 'env',
      prompt: 'Environment',
      type: 'choice',
      options: [
        { id: 'staging', label: 'Staging' },
        { id: 'prod', label: 'Production' }
      ],
      allowFreeText: false,
      default: 'staging',
      required: true
    }
  ]
};

// Test seam: the card only reaches respondToQuestion on the injected client.
const client = { respondToQuestion } as unknown as ChainlitAPI;

const renderCard = (overrides: Partial<IQuestion> = {}) =>
  render(
    <ChainlitContext.Provider value={client}>
      <MessageContext.Provider
        value={{ ...defaultMessageContext, cot: 'hidden' }}
      >
        <QuestionCard question={{ ...question, ...overrides }} />
      </MessageContext.Provider>
    </ChainlitContext.Provider>
  );

const click = (name: string | RegExp) =>
  fireEvent.click(screen.getByRole('button', { name }));

describe('QuestionForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('walks a multi-question form and validates typed fields', () => {
    renderCard();

    expect(screen.getByText('1/3')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: /Your name/ })).toHaveValue(
      'Ada'
    );
    expect(screen.getByRole('button', { name: 'Back' })).toBeDisabled();

    click('Next');
    expect(screen.getByText('2/3')).toBeInTheDocument();

    click('Next');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'This question is required'
    );
    expect(screen.getByText('2/3')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: 'not-an-email' }
    });
    click('Next');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Enter a valid email address'
    );

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: 'ada@example.com' }
    });
    click('Next');
    expect(screen.getByText('3/3')).toBeInTheDocument();

    click('Back');
    expect(screen.getByText('2/3')).toBeInTheDocument();
  });

  it('reviews defaults and submits every answer as one payload', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { answers: [] },
      revision: 2,
      durable: true
    });
    renderCard();

    click('Next');
    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: 'ada@example.com' }
    });
    click('Next');
    click('Next');

    expect(screen.getByText('Review your answers')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('ada@example.com')).toBeInTheDocument();
    expect(screen.getByText('Staging')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();

    click('Submit');

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionId: 'session-1',
        questionId: 'question-1',
        answers: [
          { questionId: 'name', text: 'Ada' },
          { questionId: 'email', text: 'ada@example.com' },
          { questionId: 'env', optionId: 'staging' }
        ]
      })
    );
    const payload = respondToQuestion.mock.calls[0][0];
    expect(payload).not.toHaveProperty('optionId');
    expect(payload).not.toHaveProperty('text');
    expect(setQuestions).toHaveBeenCalled();
  });

  it('omits an unanswered optional field', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { answers: [] },
      revision: 2,
      durable: true
    });
    const questions = [
      { ...question.questions![0] },
      {
        ...question.questions![1],
        id: 'notes',
        prompt: 'Notes',
        type: 'text' as const,
        required: false
      }
    ];
    renderCard({ questions, multi: true });

    click('Next');
    click('Next');
    expect(screen.getByText('Review your answers')).toBeInTheDocument();
    expect(screen.getByText('Not answered')).toBeInTheDocument();
    click('Submit');

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion.mock.calls[0][0].answers).toEqual([
      { questionId: 'name', text: 'Ada' }
    ]);
  });

  it('selects checkboxes and submits optionIds with other text', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { answers: [] },
      revision: 2,
      durable: true
    });
    const environments: IQuestionField = {
      id: 'envs',
      prompt: 'Environments',
      type: 'choice',
      multiple: true,
      options: [
        { id: 'staging', label: 'Staging' },
        { id: 'prod', label: 'Production' },
        { id: 'preview', label: 'Preview' }
      ],
      allowFreeText: true,
      default: ['staging', 'prod'],
      required: true
    };
    renderCard({ questions: [environments], multi: false });

    expect(screen.getByText('Select all that apply')).toBeInTheDocument();
    expect(screen.getByText('and')).toBeInTheDocument();
    expect(screen.queryByText('or')).toBeNull();
    expect(screen.getByRole('checkbox', { name: /Staging/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Production/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Preview/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole('checkbox', { name: /Production/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Preview/ }));
    fireEvent.change(screen.getByLabelText('Other answer'), {
      target: { value: '  also blue  ' }
    });
    click('Submit');

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion.mock.calls[0][0].answers).toEqual([
      {
        questionId: 'envs',
        optionIds: ['staging', 'preview'],
        text: 'also blue'
      }
    ]);
  });

  it('summarizes a multi-select answer on the review step', async () => {
    renderCard({
      questions: [
        {
          id: 'envs',
          prompt: 'Environments',
          type: 'choice',
          multiple: true,
          options: [
            { id: 'staging', label: 'Staging' },
            { id: 'prod', label: 'Production' },
            { id: 'preview', label: 'Preview' }
          ],
          allowFreeText: true,
          default: ['staging', 'prod'],
          required: true
        },
        {
          id: 'notes',
          prompt: 'Notes',
          type: 'text',
          options: [],
          allowFreeText: true,
          required: false
        }
      ],
      multi: true
    });

    expect(screen.getByRole('checkbox', { name: /Staging/ })).toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: /Production/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Preview/ }));
    fireEvent.change(screen.getByLabelText('Other answer'), {
      target: { value: 'also blue' }
    });
    click('Next');
    click('Next');

    expect(screen.getByText('Review your answers')).toBeInTheDocument();
    expect(screen.getByText('Staging, Preview, also blue')).toBeInTheDocument();
    expect(screen.getByText('Not answered')).toBeInTheDocument();
  });

  it('submits a single structured question without a wizard', async () => {
    respondToQuestion.mockResolvedValue({
      questionId: 'question-1',
      status: 'answered',
      answer: { answers: [] },
      revision: 2,
      durable: true
    });
    renderCard({ questions: [question.questions![1]], multi: false });

    expect(screen.queryByText(/1\/1/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: 'ada@example.com' }
    });
    click('Submit');

    await waitFor(() => expect(respondToQuestion).toHaveBeenCalledTimes(1));
    expect(respondToQuestion.mock.calls[0][0].answers).toEqual([
      { questionId: 'email', text: 'ada@example.com' }
    ]);
  });

  it('renders every answer on an answered card', () => {
    renderCard({
      status: 'answered',
      live: false,
      answer: {
        answers: [
          { questionId: 'name', text: null, redacted: true },
          { questionId: 'email', text: null, redacted: true },
          { questionId: 'env', optionId: 'prod', label: 'Production' }
        ]
      }
    });

    expect(screen.getAllByText('Answered privately')).toHaveLength(2);
    expect(screen.getByText('Production')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull();
  });
});
