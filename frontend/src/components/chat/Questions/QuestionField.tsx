import { getDateFnsLocale } from '@/i18n/dateLocale';
import type { IQuestionField } from '@chainlit-ng/react-client';
import { format } from 'date-fns';
import { Calendar as CalendarIcon, X } from 'lucide-react';
import { useState } from 'react';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import { Calendar } from '@/components/ui/calendar';
import {
  Popover,
  PopoverContent,
  PopoverTrigger
} from '@/components/ui/popover';

import {
  QuestionFieldError,
  QuestionFieldValue,
  parseIsoDate,
  toIsoDate
} from './fields';

const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary';

const inputClass = `mt-1 w-full rounded-md border border-border bg-background p-2 text-sm ${focusRing}`;

const errorMessage = (
  error: QuestionFieldError,
  t: (key: string, options?: { defaultValue?: string }) => string
): string => {
  switch (error) {
    case 'required':
      return t('chat.question.errors.required');
    case 'email':
      return t('chat.question.errors.email');
    case 'date':
      return t('chat.question.errors.date');
    case 'number':
      return t('chat.question.errors.number');
    case 'url':
      return t('chat.question.errors.url');
    case 'tel':
      return t('chat.question.errors.tel');
  }
};

interface Props {
  field: IQuestionField;
  formId: string;
  value: QuestionFieldValue;
  error?: QuestionFieldError | null;
  disabled?: boolean;
  onChange: (value: QuestionFieldValue) => void;
}

const DateField = ({
  field,
  inputId,
  promptId,
  value,
  disabled,
  onChange
}: {
  field: IQuestionField;
  inputId: string;
  promptId: string;
  value: QuestionFieldValue;
  disabled?: boolean;
  onChange: (value: QuestionFieldValue) => void;
}) => {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const locale = getDateFnsLocale(i18n.language);
  const selected = parseIsoDate(value.text);

  return (
    <div className="mt-1 flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={inputId}
            type="button"
            variant="outline"
            disabled={disabled}
            aria-labelledby={promptId}
            className="w-full justify-start text-left font-normal"
          >
            <CalendarIcon className="size-4" />
            {selected ? (
              <span>{format(selected, 'PPP', { locale })}</span>
            ) : (
              <span className="text-muted-foreground">
                {field.placeholder ?? t('chat.question.datePlaceholder')}
              </span>
            )}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          container={document.body}
          className="w-auto p-0"
          align="start"
        >
          <Calendar
            mode="single"
            selected={selected}
            onSelect={(date) => {
              if (!date) return;
              onChange({ ...value, text: toIsoDate(date) });
              setOpen(false);
            }}
            locale={locale}
            showOutsideDays={false}
            autoFocus
          />
        </PopoverContent>
      </Popover>
      {!field.required && value.text ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          disabled={disabled}
          aria-label={t('chat.question.clear')}
          onClick={() => onChange({ ...value, text: '' })}
        >
          <X className="size-4" />
        </Button>
      ) : null}
    </div>
  );
};

export const QuestionField = ({
  field,
  formId,
  value,
  error,
  disabled,
  onChange
}: Props) => {
  const { t } = useTranslation();
  const promptId = `${formId}-${field.id}-prompt`;
  const inputId = `${formId}-${field.id}-input`;
  const errorId = `${formId}-${field.id}-error`;
  const showError = Boolean(error);

  const control = (() => {
    if (field.type === 'choice') {
      const textareaId = `${formId}-${field.id}-other`;
      return (
        <>
          <fieldset className="mt-2">
            <legend className="sr-only">{field.prompt}</legend>
            <div className="flex flex-col gap-2">
              {field.options.map((option) => {
                const descriptionId = option.description
                  ? `${formId}-${field.id}-${option.id}-description`
                  : undefined;
                return (
                  <label
                    key={option.id}
                    className="flex min-h-11 cursor-pointer items-start gap-2 rounded-md border border-border p-2.5 text-sm transition-colors hover:bg-muted/40 has-[:checked]:border-primary has-[:checked]:bg-primary/5"
                  >
                    <input
                      type={field.multiple ? 'checkbox' : 'radio'}
                      name={`${formId}-${field.id}`}
                      value={option.id}
                      checked={
                        field.multiple
                          ? value.optionIds.includes(option.id)
                          : value.optionId === option.id
                      }
                      disabled={disabled}
                      aria-describedby={descriptionId}
                      onChange={() => {
                        if (field.multiple) {
                          const optionIds = value.optionIds.includes(option.id)
                            ? value.optionIds.filter((id) => id !== option.id)
                            : [...value.optionIds, option.id];
                          onChange({
                            optionId: undefined,
                            optionIds,
                            text: value.text
                          });
                          return;
                        }
                        onChange({
                          optionId: option.id,
                          optionIds: [],
                          text: ''
                        });
                      }}
                      className={`mt-0.5 accent-primary ${
                        field.multiple ? 'h-4 w-4 rounded' : ''
                      } ${focusRing}`}
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
          {field.allowFreeText ? (
            <>
              <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                <span className="h-px flex-1 bg-border" />
                <span>
                  {field.multiple
                    ? t('chat.question.and', { defaultValue: 'and' })
                    : t('chat.question.or', { defaultValue: 'or' })}
                </span>
                <span className="h-px flex-1 bg-border" />
              </div>
              <textarea
                id={textareaId}
                aria-label={t('chat.question.otherAriaLabel')}
                aria-describedby={showError ? errorId : undefined}
                placeholder={
                  field.placeholder ??
                  t('chat.question.otherPlaceholder', {
                    defaultValue: 'Type your answer...'
                  })
                }
                className={`min-h-16 ${inputClass}`}
                maxLength={2000}
                disabled={disabled}
                value={value.text}
                onChange={(event) =>
                  onChange({
                    optionId: undefined,
                    optionIds: field.multiple ? value.optionIds : [],
                    text: event.target.value
                  })
                }
              />
            </>
          ) : null}
        </>
      );
    }

    if (field.type === 'date') {
      return (
        <DateField
          field={field}
          inputId={inputId}
          promptId={promptId}
          value={value}
          disabled={disabled}
          onChange={onChange}
        />
      );
    }

    const inputProps = {
      id: inputId,
      'aria-labelledby': promptId,
      'aria-describedby': showError ? errorId : undefined,
      placeholder:
        field.placeholder ??
        t('chat.question.otherPlaceholder', {
          defaultValue: 'Type your answer...'
        }),
      className: inputClass,
      maxLength: 2000,
      disabled,
      value: value.text,
      onChange: (
        event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
      ) =>
        onChange({
          optionId: undefined,
          optionIds: [],
          text: event.target.value
        })
    };

    if (field.type === 'text' && field.multiline !== false) {
      return <textarea {...inputProps} className={`min-h-16 ${inputClass}`} />;
    }

    switch (field.type) {
      case 'email':
        return <input type="email" autoComplete="email" {...inputProps} />;
      case 'number':
        return (
          <input type="number" step="any" inputMode="decimal" {...inputProps} />
        );
      case 'url':
        return <input type="url" autoComplete="url" {...inputProps} />;
      case 'tel':
        return (
          <input
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            {...inputProps}
          />
        );
      default:
        return <input type="text" {...inputProps} />;
    }
  })();

  return (
    <div data-testid={`question-field-${field.id}`} className="mt-3 first:mt-0">
      <div id={promptId} className="text-sm font-medium leading-6">
        <bdi dir="auto">{field.prompt}</bdi>
        {field.required ? (
          <span className="ml-1 text-destructive" aria-hidden="true">
            *
          </span>
        ) : (
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {t('chat.question.optional')}
          </span>
        )}
      </div>
      {field.multiple ? (
        <div className="mt-1 text-xs text-muted-foreground">
          {t('chat.question.selectMultiple')}
        </div>
      ) : null}
      {control}
      {error ? (
        <div
          id={errorId}
          role="alert"
          className="mt-1 text-xs text-destructive"
        >
          {errorMessage(error, t)}
        </div>
      ) : null}
    </div>
  );
};
