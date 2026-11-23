import type {
  IQuestionAnswerItem,
  IQuestionField,
  IQuestionFieldAnswer,
  QuestionStatus
} from '@chainlit-ng/react-client';
import { format, isValid, parseISO } from 'date-fns';

export interface QuestionFieldValue {
  optionId?: string;
  text: string;
}

export type QuestionFieldError =
  'required' | 'email' | 'date' | 'number' | 'url' | 'tel';

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const TEL_PATTERN = /^[0-9+().\-\s]{3,32}$/;

export const emptyFieldValue = (field: IQuestionField): QuestionFieldValue => {
  const fallback = field.default ?? '';
  if (field.type === 'choice') {
    return {
      optionId: fallback || undefined,
      text: ''
    };
  }
  return { optionId: undefined, text: fallback };
};

export const validateField = (
  field: IQuestionField,
  value: QuestionFieldValue
): QuestionFieldError | null => {
  const text = value.text.trim();
  if (field.type === 'choice') {
    if (value.optionId) return null;
    if (text) return null;
    return field.required ? 'required' : null;
  }
  if (!text) return field.required ? 'required' : null;
  switch (field.type) {
    case 'email':
      return EMAIL_PATTERN.test(text) ? null : 'email';
    case 'date':
      return DATE_PATTERN.test(text) && isValid(parseISO(text)) ? null : 'date';
    case 'number':
      return NUMBER_PATTERN.test(text) && Number.isFinite(Number(text))
        ? null
        : 'number';
    case 'url': {
      try {
        const url = new URL(text);
        return url.protocol === 'http:' || url.protocol === 'https:'
          ? null
          : 'url';
      } catch {
        return 'url';
      }
    }
    case 'tel':
      return TEL_PATTERN.test(text) ? null : 'tel';
    default:
      return null;
  }
};

export const buildAnswerItems = (
  fields: IQuestionField[],
  values: Record<string, QuestionFieldValue>
): IQuestionAnswerItem[] => {
  const answers: IQuestionAnswerItem[] = [];
  for (const field of fields) {
    const value = values[field.id];
    if (!value) continue;
    if (field.type === 'choice' && value.optionId) {
      answers.push({ questionId: field.id, optionId: value.optionId });
      continue;
    }
    const text = value.text.trim();
    if (text) answers.push({ questionId: field.id, text });
  }
  return answers;
};

export const fieldSummary = (
  field: IQuestionField,
  value: QuestionFieldValue | undefined
): string | null => {
  if (!value) return null;
  if (field.type === 'choice' && value.optionId) {
    const option = field.options.find((item) => item.id === value.optionId);
    return option?.label ?? value.optionId;
  }
  const text = value.text.trim();
  return text || null;
};

export const parseIsoDate = (value: string | null | undefined) => {
  if (!value || !DATE_PATTERN.test(value)) return undefined;
  const parsed = parseISO(value);
  return isValid(parsed) ? parsed : undefined;
};

export const toIsoDate = (value: Date): string => format(value, 'yyyy-MM-dd');

export const fieldAnswerSummary = (
  item: IQuestionFieldAnswer | undefined,
  answeredPrivately: string,
  notAnswered: string
): { text: string; muted: boolean } => {
  if (!item) return { text: notAnswered, muted: true };
  if (item.redacted || (item.text == null && item.optionId == null)) {
    return { text: answeredPrivately, muted: true };
  }
  if (item.label) return { text: item.label, muted: false };
  if (item.text) return { text: item.text, muted: false };
  return { text: answeredPrivately, muted: true };
};

export const questionStatusLabel = (
  status: QuestionStatus,
  t: (key: string) => string
): string => {
  switch (status) {
    case 'pending':
      return t('chat.question.status.pending');
    case 'answered':
      return t('chat.question.status.answered');
    case 'expired':
      return t('chat.question.status.expired');
    case 'superseded':
      return t('chat.question.status.superseded');
    case 'orphaned':
      return t('chat.question.status.orphaned');
    default:
      return status;
  }
};
