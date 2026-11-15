import { formatLocalDateTime } from '@/lib/datetime';
import {
  ChainlitContext,
  IQuestion,
  IQuestionOption,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import { useContext, useState } from 'react';
import { toast } from 'sonner';
import { v4 as uuidv4 } from 'uuid';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

// Controls sit in a scrollable card body, where an outward focus ring is
// clipped. Draw the ring inside the control instead.
const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary';

const questionErrorMessage = (error: unknown, fallback: string): string => {
  const detail = (error as { payload?: { detail?: unknown } })?.payload?.detail;
  if (typeof detail === 'string' && detail) return detail;
  return error instanceof Error ? error.message : fallback;
};

interface Props {
  question: IQuestion;
}

export const QuestionCard = ({ question }: Props) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const setQuestions = useSetChainlitState('questions');
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | undefined>();
  const [text, setText] = useState('');

  const isPending = question.status === 'pending';
  const isLive = question.live === true;
  const canAnswer = isPending && isLive;
  const answer = question.answer;
  const decidedAtIso = question.decidedAt ?? null;
  const decidedAtLabel = formatLocalDateTime(decidedAtIso);
  const submitDisabled = busy || (!selected && !text.trim());

  const submit = async () => {
    if (!selected && !text.trim()) return;
    setBusy(true);
    try {
      const response = await apiClient.respondToQuestion({
        sessionId,
        questionId: question.id,
        ...(selected ? { optionId: selected } : { text: text.trim() }),
        decisionRequestId: uuidv4()
      });
      setQuestions((old) =>
        old.map((item) =>
          item.id === response.questionId
            ? {
                ...item,
                status: response.status,
                answer: response.answer ?? item.answer,
                decidedBy: response.answeredBy ?? item.decidedBy,
                decidedAt: response.answeredAt ?? item.decidedAt,
                revision: response.revision
              }
            : item
        )
      );
    } catch (error) {
      toast.error(
        questionErrorMessage(error, t('chat.question.errors.answer'))
      );
    } finally {
      setBusy(false);
    }
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
        <div className="whitespace-pre-wrap rounded-md bg-muted/40 p-2.5 text-sm leading-6">
          <bdi dir="auto">{question.prompt}</bdi>
        </div>

        {canAnswer ? (
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
        ) : (
          <div className="mt-3 rounded-md bg-muted/40 p-3 text-sm text-muted-foreground">
            {question.status === 'answered' ? (
              <span>
                {answer?.redacted ? (
                  <span>{t('chat.question.answeredPrivately')}</span>
                ) : answer?.label ? (
                  <bdi dir="auto">{answer.label}</bdi>
                ) : answer?.text ? (
                  <bdi dir="auto" className="whitespace-pre-wrap">
                    {answer.text}
                  </bdi>
                ) : null}
              </span>
            ) : (
              <span>{t('chat.question.readOnly')}</span>
            )}
            {question.decidedBy ? (
              <span>
                {' '}
                · {t('chat.question.by')}{' '}
                <bdi dir="auto">{question.decidedBy}</bdi>
              </span>
            ) : null}
            {decidedAtLabel ? (
              <span className="inline-block">
                {' · '}
                <time
                  className="whitespace-nowrap"
                  dateTime={decidedAtIso ?? undefined}
                  title={decidedAtIso ?? ''}
                >
                  {decidedAtLabel}
                </time>
              </span>
            ) : null}
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
