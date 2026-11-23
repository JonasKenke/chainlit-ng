import type { IQuestion, IQuestionField } from '@chainlit-ng/react-client';
import { useState } from 'react';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

import { QuestionField } from './QuestionField';
import { QuestionSummary } from './QuestionSummary';
import {
  QuestionFieldError,
  QuestionFieldValue,
  buildAnswerItems,
  emptyFieldValue,
  fieldAnswerSummary,
  fieldSummary,
  questionStatusLabel,
  validateField
} from './fields';
import { useQuestionAnswer } from './useQuestionAnswer';

export const QuestionForm = ({ question }: { question: IQuestion }) => {
  const { t } = useTranslation();
  const fields = question.questions ?? [];
  const { answer, busy } = useQuestionAnswer(question);
  const [values, setValues] = useState<Record<string, QuestionFieldValue>>(() =>
    Object.fromEntries(
      fields.map((field) => [field.id, emptyFieldValue(field)])
    )
  );
  const [errors, setErrors] = useState<
    Record<string, QuestionFieldError | null>
  >({});
  const [step, setStep] = useState(0);

  const isMulti = fields.length > 1;
  const isReview = step === fields.length;
  const isPending = question.status === 'pending';
  const isLive = question.live === true;
  const canAnswer = isPending && isLive;
  const current = fields[step];

  const answeredItems = new Map(
    (question.answer?.answers ?? []).map((item) => [item.questionId, item])
  );

  const setFieldValue = (field: IQuestionField, value: QuestionFieldValue) => {
    setValues((old) => ({ ...old, [field.id]: value }));
    setErrors((old) => {
      if (!(field.id in old)) return old;
      const updated = { ...old };
      const nextError = validateField(field, value);
      if (nextError) updated[field.id] = nextError;
      else delete updated[field.id];
      return updated;
    });
  };

  const goNext = () => {
    if (!current) return;
    const value = values[current.id] ?? emptyFieldValue(current);
    const error = validateField(current, value);
    if (error) {
      setErrors((old) => ({ ...old, [current.id]: error }));
      return;
    }
    setStep((old) => Math.min(old + 1, fields.length));
  };

  const goBack = () => {
    setStep((old) => Math.max(0, old - 1));
  };

  const submit = async () => {
    if (busy) return;
    for (let index = 0; index < fields.length; index += 1) {
      const field = fields[index];
      const error = validateField(
        field,
        values[field.id] ?? emptyFieldValue(field)
      );
      if (error) {
        setErrors((old) => ({ ...old, [field.id]: error }));
        setStep(index);
        return;
      }
    }
    await answer({ answers: buildAnswerItems(fields, values) });
  };

  const renderReadOnly = () => {
    if (question.status !== 'answered') {
      const status = isLive ? question.status : 'orphaned';
      return (
        <div className="mt-3 text-xs">
          <span className="font-medium text-foreground">
            {questionStatusLabel(status, t)}
          </span>
        </div>
      );
    }
    return (
      <QuestionSummary
        rows={fields.map((field) => {
          const summary = fieldAnswerSummary(
            answeredItems.get(field.id),
            t('chat.question.answeredPrivately'),
            t('chat.question.notAnswered')
          );
          return {
            key: field.id,
            prompt: field.prompt,
            answer: summary.text,
            muted: summary.muted
          };
        })}
        emptyLabel={t('chat.question.notAnswered')}
      />
    );
  };

  const renderReview = () => (
    <div className="flex flex-col">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t('chat.question.reviewTitle')}
      </div>
      <QuestionSummary
        rows={fields.map((field) => {
          const summary = fieldSummary(
            field,
            values[field.id] ?? emptyFieldValue(field)
          );
          return {
            key: field.id,
            prompt: field.prompt,
            answer: summary,
            muted: summary === null
          };
        })}
        emptyLabel={t('chat.question.notAnswered')}
        onEdit={(key) => {
          const index = fields.findIndex((field) => field.id === key);
          if (index !== -1) setStep(index);
        }}
      />
    </div>
  );

  return (
    <section
      data-testid="question-card"
      data-question-id={question.id}
      aria-label={t('chat.question.ariaLabel')}
      aria-live="polite"
      className="scroll-mt-4 flex max-h-[min(80vh,calc(100dvh_-_14rem))] flex-col rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="flex shrink-0 items-baseline justify-between gap-3">
        <div className="text-sm font-semibold">{t('chat.question.title')}</div>
        {isMulti && canAnswer ? (
          <div
            data-testid="question-card-progress"
            className="text-xs tabular-nums text-muted-foreground"
          >
            {t('chat.question.progress', {
              current: isReview ? fields.length : step + 1,
              total: fields.length
            })}
          </div>
        ) : null}
      </div>

      <div
        data-testid="question-card-scroll"
        className="mt-3 min-h-0 flex-1 overflow-y-auto custom-scrollbar pr-1"
      >
        {canAnswer ? (
          isReview ? (
            renderReview()
          ) : current ? (
            <QuestionField
              field={current}
              formId={question.id}
              value={values[current.id] ?? emptyFieldValue(current)}
              error={errors[current.id]}
              disabled={busy}
              onChange={(value) => setFieldValue(current, value)}
            />
          ) : null
        ) : (
          renderReadOnly()
        )}
      </div>

      {canAnswer ? (
        <div
          data-testid="question-card-actions"
          className={`mt-3 flex shrink-0 gap-2 border-t border-border pt-3 ${
            isMulti ? 'justify-between' : 'justify-end'
          }`}
        >
          {isMulti ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy || step === 0}
              onClick={goBack}
            >
              {t('chat.question.back')}
            </Button>
          ) : null}
          {isReview ? (
            <Button
              type="button"
              size="sm"
              className="min-w-20"
              disabled={busy}
              onClick={submit}
            >
              {t('common.actions.submit')}
            </Button>
          ) : isMulti ? (
            <Button
              type="button"
              size="sm"
              className="min-w-20"
              disabled={busy}
              onClick={goNext}
            >
              {t('chat.question.next')}
            </Button>
          ) : (
            <Button
              type="button"
              size="sm"
              className="w-full sm:w-auto"
              disabled={busy}
              onClick={submit}
            >
              {t('common.actions.submit')}
            </Button>
          )}
        </div>
      ) : null}
    </section>
  );
};
