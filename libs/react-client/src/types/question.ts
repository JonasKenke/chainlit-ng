export type QuestionStatus =
  'pending' | 'answered' | 'expired' | 'superseded' | 'orphaned';

export type QuestionFieldType =
  'text' | 'email' | 'date' | 'number' | 'url' | 'tel' | 'choice';

export interface IQuestionOption {
  id: string;
  label: string;
  description?: string | null;
}

export interface IQuestionField {
  id: string;
  prompt: string;
  type: QuestionFieldType;
  multiple?: boolean;
  options: IQuestionOption[];
  allowFreeText: boolean;
  default?: string | string[] | null;
  required: boolean;
  placeholder?: string | null;
  multiline?: boolean;
}

export interface IQuestionFieldAnswer {
  questionId: string;
  optionId?: string | null;
  label?: string | null;
  optionIds?: string[] | null;
  labels?: string[] | null;
  text?: string | null;
  redacted?: boolean;
}

export interface IQuestionAnswer {
  optionId?: string | null;
  label?: string | null;
  text?: string | null;
  redacted?: boolean;
  answers?: IQuestionFieldAnswer[];
}

export interface IQuestion {
  kind?: 'question';
  id: string;
  stepId: string;
  threadId: string;
  status: QuestionStatus;
  prompt: string;
  options: IQuestionOption[];
  allowFreeText: boolean;
  questions?: IQuestionField[];
  structured?: boolean;
  multi?: boolean;
  requestedAt?: string | null;
  expiresAt?: string | null;
  revision?: number;
  decidedBy?: string | null;
  decidedAt?: string | null;
  answer?: IQuestionAnswer | null;
  terminalReason?: string | null;
  terminalAt?: string | null;
  /** Set by a live socket event; hydrated/read-only records stay false. */
  live?: boolean;
}

export interface IQuestionAnswerItem {
  questionId: string;
  optionId?: string;
  optionIds?: string[];
  text?: string;
}

export interface IQuestionAnswerPayload {
  sessionId: string;
  questionId: string;
  optionId?: string;
  text?: string;
  answers?: IQuestionAnswerItem[];
  decisionRequestId: string;
}

export interface IQuestionAnswerResponse {
  questionId: string;
  status: QuestionStatus;
  answer?: IQuestionAnswer | null;
  answeredBy?: string | null;
  answeredAt?: string | null;
  revision: number;
  durable: boolean;
  error?: string | null;
}
