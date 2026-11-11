import { useChainlitStore } from '@chainlit/react-client';

import { ApprovalCard } from '@/components/chat/Approvals/ApprovalCard';
import { QuestionCard } from '@/components/chat/Questions/QuestionCard';

interface Props {
  stepId: string;
  threadId?: string;
}

/**
 * Render the interactions bound to one step at that step's position in the
 * message flow. A question asked by an on_message run belongs here, between
 * the user's input and the assistant's answer.
 */
export function StepInteractions({ stepId, threadId }: Props) {
  const approvals = useChainlitStore((state) => state.approvals) ?? [];
  const questions = useChainlitStore((state) => state.questions) ?? [];
  const stepApprovals = approvals.filter(
    (approval) => approval.stepId === stepId
  );
  const stepQuestions = questions.filter(
    (question) =>
      question.stepId === stepId &&
      (question.live || question.threadId === threadId)
  );

  if (!stepApprovals.length && !stepQuestions.length) return null;

  return (
    <div className="flex flex-col gap-2 py-2">
      {stepApprovals.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} />
      ))}
      {stepQuestions.map((question) => (
        <QuestionCard key={question.id} question={question} />
      ))}
    </div>
  );
}
