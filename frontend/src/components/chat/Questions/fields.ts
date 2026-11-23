import type {
  IQuestionAnswerItem,
  IQuestionField,
  IQuestionFieldAnswer,
  QuestionStatus
} from '@chainlit-ng/react-client';
import { format, isValid, parseISO } from 'date-fns';

export interface QuestionFieldValue {
  optionId?: string;
  optionIds: string[];
  text: string;
}

export type QuestionFieldError =
  'required' | 'email' | 'date' | 'number' | 'url' | 'tel';

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const TEL_PATTERN = /^[0-9+().\-\s]{3,32}$/;

export const emptyFieldValue = (field: IQuestionField): QuestionFieldValue => {
  const defaults = Array.isArray(field.default)
    ? field.default
    : field.default
      ? [field.default]
      : [];
  if (field.type === 'choice') {
    if (field.multiple) {
      return { optionId: undefined, optionIds: defaults, text: '' };
    }
    return { optionId: defaults[0], optionIds: [], text: '' };
  }
  return {
    optionId: undefined,
    optionIds: [],
    text: typeof field.default === 'string' ? field.default : ''
  };
};

export const validateField = (
  field: IQuestionField,
  value: QuestionFieldValue
): QuestionFieldError | null => {
  const text = value.text.trim();
  if (field.type === 'choice') {
    if (field.multiple ? value.optionIds.length > 0 : value.optionId) {
      return null;
    }
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
    const text = value.text.trim();
    if (field.type === 'choice' && field.multiple) {
      if (value.optionIds.length || text) {
        answers.push({
          questionId: field.id,
          ...(value.optionIds.length ? { optionIds: value.optionIds } : {}),
          ...(text ? { text } : {})
        });
      }
      continue;
    }
    if (field.type === 'choice' && value.optionId) {
      answers.push({ questionId: field.id, optionId: value.optionId });
      continue;
    }
    if (text) answers.push({ questionId: field.id, text });
  }
  return answers;
};

export const fieldSummary = (
  field: IQuestionField,
  value: QuestionFieldValue | undefined
): string | null => {
  if (!value) return null;
  if (field.type === 'choice') {
    if (field.multiple) {
      const labels = value.optionIds.map(
        (id) => field.options.find((item) => item.id === id)?.label ?? id
      );
      const text = value.text.trim();
      const parts = [...labels, ...(text ? [text] : [])];
      return parts.length ? parts.join(', ') : null;
    }
    if (value.optionId) {
      const option = field.options.find((item) => item.id === value.optionId);
      return option?.label ?? value.optionId;
    }
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
  const selected = item.labels?.length ? item.labels : (item.optionIds ?? []);
  if (selected.length) {
    const parts = [...selected];
    if (item.redacted) parts.push(answeredPrivately);
    else if (item.text) parts.push(item.text);
    return { text: parts.join(', '), muted: false };
  }
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
