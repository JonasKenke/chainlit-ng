import {
  ChainlitContext,
  IQuestion,
  IQuestionAnswerPayload,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import { useCallback, useContext, useState } from 'react';
import { toast } from 'sonner';
import { v4 as uuidv4 } from 'uuid';

import { useTranslation } from '@/components/i18n/Translator';

const payloadDetail = (error: unknown): string | null => {
  if (!error || typeof error !== 'object' || !('payload' in error)) return null;
  const payload = error.payload;
  if (!payload || typeof payload !== 'object' || !('detail' in payload)) {
    return null;
  }
  return typeof payload.detail === 'string' && payload.detail
    ? payload.detail
    : null;
};

const questionErrorMessage = (error: unknown, fallback: string): string => {
  const detail = payloadDetail(error);
  if (detail) return detail;
  return error instanceof Error ? error.message : fallback;
};

type AnswerBody = Omit<
  IQuestionAnswerPayload,
  'sessionId' | 'questionId' | 'decisionRequestId'
>;

export const useQuestionAnswer = (question: IQuestion) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const setQuestions = useSetChainlitState('questions');
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  const answer = useCallback(
    async (body: AnswerBody): Promise<boolean> => {
      setBusy(true);
      try {
        const response = await apiClient.respondToQuestion({
          sessionId,
          questionId: question.id,
          ...body,
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
        return true;
      } catch (error) {
        toast.error(
          questionErrorMessage(error, t('chat.question.errors.answer'))
        );
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apiClient, question.id, sessionId, setQuestions, t]
  );

  return { answer, busy };
};
