import { useChainlitStore } from '@chainlit-ng/react-client';

import { ApprovalCard } from '@/components/chat/Approvals/ApprovalCard';
import { ApprovalQueueCard } from '@/components/chat/Approvals/ApprovalQueueCard';
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
  const pendingApprovals = approvals.filter(
    (approval) =>
      approval.status === 'pending' &&
      approval.live !== false &&
      (!threadId || approval.threadId === threadId)
  );
  const groupedApprovals = pendingApprovals.length > 1;
  const groupedIds = new Set(
    groupedApprovals ? pendingApprovals.map((approval) => approval.id) : []
  );
  const showQueue = groupedApprovals && pendingApprovals[0]?.stepId === stepId;
  const stepApprovals = approvals.filter(
    (approval) => approval.stepId === stepId && !groupedIds.has(approval.id)
  );
  const stepQuestions = questions.filter(
    (question) =>
      question.stepId === stepId &&
      (question.live || question.threadId === threadId)
  );

  if (!showQueue && !stepApprovals.length && !stepQuestions.length) return null;

  return (
    <div className="flex flex-col gap-2 py-2">
      {showQueue ? <ApprovalQueueCard approvals={pendingApprovals} /> : null}
      {stepApprovals.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} />
      ))}
      {stepQuestions.map((question) => (
        <QuestionCard key={question.id} question={question} />
      ))}
    </div>
  );
}
