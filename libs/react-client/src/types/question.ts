export type QuestionStatus =
  'pending' | 'answered' | 'expired' | 'superseded' | 'orphaned';

export interface IQuestionOption {
  id: string;
  label: string;
  description?: string | null;
}

export interface IQuestionAnswer {
  optionId?: string | null;
  label?: string | null;
  text?: string | null;
  redacted?: boolean;
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

export interface IQuestionAnswerPayload {
  sessionId: string;
  questionId: string;
  optionId?: string;
  text?: string;
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
