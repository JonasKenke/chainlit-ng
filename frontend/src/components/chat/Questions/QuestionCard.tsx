import { useContext, useState } from 'react';
import { toast } from 'sonner';
import { v4 as uuidv4 } from 'uuid';

import {
  ChainlitContext,
  IQuestion,
  IQuestionOption,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit/react-client';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

const statusKey: Record<IQuestion['status'], string> = {
  pending: 'chat.question.status.pending',
  answered: 'chat.question.status.answered',
  expired: 'chat.question.status.expired',
  superseded: 'chat.question.status.superseded',
  orphaned: 'chat.question.status.orphaned'
};

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
      aria-label={t('chat.question.ariaLabel')}
      aria-live="polite"
      className="rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="text-sm font-semibold">{t('chat.question.title')}</div>
        <span className="text-xs font-medium uppercase text-muted-foreground">
          {t(statusKey[question.status])}
        </span>
      </div>

      <p className="mt-2 whitespace-pre-wrap text-sm">
        <bdi dir="auto">{question.prompt}</bdi>
      </p>

      {canAnswer ? (
        <>
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
                      className="flex min-h-11 cursor-pointer items-start gap-2 rounded border border-border p-2 text-sm"
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
                        className="mt-1"
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

          {question.allowFreeText ? (
            <div className="mt-3">
              <label
                htmlFor={`question-${question.id}-other`}
                className="text-xs font-medium text-muted-foreground"
              >
                {t('chat.question.other')}
              </label>
              <textarea
                id={`question-${question.id}-other`}
                aria-label={t('chat.question.otherAriaLabel')}
                className="mt-1 min-h-20 w-full rounded border border-border bg-background p-2 text-sm"
                maxLength={2000}
                value={text}
                onChange={(event) => {
                  setText(event.target.value);
                  if (event.target.value) setSelected(undefined);
                }}
              />
            </div>
          ) : null}

          <div className="mt-3">
            <Button
              type="button"
              size="sm"
              disabled={submitDisabled}
              onClick={submit}
            >
              {t('common.actions.submit')}
            </Button>
          </div>
        </>
      ) : (
        <div className="mt-3 text-sm text-muted-foreground">
          {isPending ? (
            <span>{t('chat.question.readOnly')}</span>
          ) : question.status === 'answered' ? (
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
            <span>{t(statusKey[question.status])}</span>
          )}
          {question.decidedBy ? (
            <span>
              {' '}
              · {t('chat.question.by')}{' '}
              <bdi dir="auto">{question.decidedBy}</bdi>
            </span>
          ) : null}
          {question.decidedAt ? <span> · {question.decidedAt}</span> : null}
        </div>
      )}
    </section>
  );
};
