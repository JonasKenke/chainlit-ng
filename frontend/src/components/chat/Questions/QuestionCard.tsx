import { IQuestion, IQuestionOption } from '@chainlit-ng/react-client';
import { useState } from 'react';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

import { QuestionForm } from './QuestionForm';
import { QuestionSummary } from './QuestionSummary';
import { fieldAnswerSummary, questionStatusLabel } from './fields';
import { useQuestionAnswer } from './useQuestionAnswer';

// Controls sit in a scrollable card body, where an outward focus ring is
// clipped. Draw the ring inside the control instead.
const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary';

interface Props {
  question: IQuestion;
}

const SingleQuestionCard = ({ question }: Props) => {
  const { t } = useTranslation();
  const { answer: sendAnswer, busy } = useQuestionAnswer(question);
  const [selected, setSelected] = useState<string | undefined>();
  const [text, setText] = useState('');

  const isPending = question.status === 'pending';
  const isLive = question.live === true;
  const canAnswer = isPending && isLive;
  const answer = question.answer;
  const submitDisabled = busy || (!selected && !text.trim());
  const answerSummary = fieldAnswerSummary(
    answer
      ? {
          questionId: question.id,
          optionId: answer.optionId,
          label: answer.label,
          text: answer.text,
          redacted: answer.redacted
        }
      : undefined,
    t('chat.question.answeredPrivately'),
    t('chat.question.notAnswered')
  );

  const submit = async () => {
    if (!selected && !text.trim()) return;
    await sendAnswer(selected ? { optionId: selected } : { text: text.trim() });
  };

  return (
    <section
      data-testid="question-card"
      data-question-id={question.id}
      aria-label={t('chat.question.ariaLabel')}
      aria-live="polite"
      className="scroll-mt-4 flex max-h-[min(80vh,calc(100dvh_-_14rem))] flex-col rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="shrink-0 text-sm font-semibold">
        {t('chat.question.title')}
      </div>

      <div
        data-testid="question-card-scroll"
        className="mt-3 min-h-0 flex-1 overflow-y-auto custom-scrollbar pr-1"
      >
        {canAnswer ? (
          <>
            <div className="whitespace-pre-wrap text-sm leading-6">
              <bdi dir="auto">{question.prompt}</bdi>
            </div>
            <div>
              {question.options.length ? (
                <fieldset className="mt-3">
                  <legend className="text-xs font-medium text-muted-foreground">
                    {t('chat.question.options')}
                  </legend>
                  <div className="mt-1 flex flex-col gap-2">
                    {question.options.map((option: IQuestionOption) => {
                      const descriptionId = option.description
                        ? `question-${question.id}-${option.id}-description`
                        : undefined;
                      return (
                        <label
                          key={option.id}
                          className="flex min-h-11 cursor-pointer items-start gap-2 rounded-md border border-border p-2.5 text-sm transition-colors hover:bg-muted/40 has-[:checked]:border-primary has-[:checked]:bg-primary/5"
                        >
                          <input
                            type="radio"
                            name={`question-${question.id}`}
                            value={option.id}
                            checked={selected === option.id}
                            aria-describedby={descriptionId}
                            onChange={() => {
                              setSelected(option.id);
                              setText('');
                            }}
                            className={`mt-0.5 accent-primary ${focusRing}`}
                          />
                          <span className="min-w-0">
                            <bdi dir="auto" className="break-words">
                              {option.label}
                            </bdi>
                            {option.description ? (
                              <span
                                id={descriptionId}
                                className="block text-xs text-muted-foreground"
                              >
                                <bdi dir="auto">{option.description}</bdi>
                              </span>
                            ) : null}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </fieldset>
              ) : null}

              {question.options.length && question.allowFreeText ? (
                <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="h-px flex-1 bg-border" />
                  <span>{t('chat.question.or', { defaultValue: 'or' })}</span>
                  <span className="h-px flex-1 bg-border" />
                </div>
              ) : null}

              {question.allowFreeText ? (
                <div className="mt-2">
                  <label
                    htmlFor={`question-${question.id}-other`}
                    className="text-xs font-medium text-muted-foreground"
                  >
                    {question.options.length
                      ? t('chat.question.other')
                      : t('chat.question.answer')}
                  </label>
                  <textarea
                    id={`question-${question.id}-other`}
                    aria-label={
                      question.options.length
                        ? t('chat.question.otherAriaLabel')
                        : t('chat.question.answerAriaLabel')
                    }
                    placeholder={t('chat.question.otherPlaceholder', {
                      defaultValue: 'Type your answer...'
                    })}
                    className={`mt-1 min-h-16 w-full rounded-md border border-border bg-background p-2 text-sm ${focusRing}`}
                    maxLength={2000}
                    value={text}
                    onChange={(event) => {
                      setText(event.target.value);
                      if (event.target.value) setSelected(undefined);
                    }}
                  />
                </div>
              ) : null}
            </div>
          </>
        ) : question.status === 'answered' ? (
          <QuestionSummary
            rows={[
              {
                key: question.id,
                prompt: question.prompt,
                answer: answerSummary.text,
                muted: answerSummary.muted
              }
            ]}
            emptyLabel={t('chat.question.notAnswered')}
          />
        ) : (
          <div className="mt-3 text-xs">
            <span className="font-medium text-foreground">
              {questionStatusLabel(isLive ? question.status : 'orphaned', t)}
            </span>
          </div>
        )}
      </div>

      {canAnswer ? (
        <div
          data-testid="question-card-actions"
          className="mt-3 flex shrink-0 justify-end border-t border-border pt-3"
        >
          <Button
            type="button"
            size="sm"
            className="w-full sm:w-auto"
            disabled={submitDisabled}
            onClick={submit}
          >
            {t('common.actions.submit')}
          </Button>
        </div>
      ) : null}
    </section>
  );
};

export const QuestionCard = ({ question }: Props) =>
  question.structured && question.questions?.length ? (
    <QuestionForm question={question} />
  ) : (
    <SingleQuestionCard question={question} />
  );
