import {
  IApproval,
  IQuestion,
  useChainlitStore
} from '@chainlit-ng/react-client';

export interface PendingInteractionState {
  approvals?: IApproval[] | null;
  questions?: IQuestion[] | null;
}

/**
 * A live approval or question that the user still has to answer.
 * Hydrated/read-only records must not block navigation.
 */
export function hasPendingInteraction({
  approvals,
  questions
}: PendingInteractionState): boolean {
  return (
    (approvals ?? []).some(
      (approval) => approval.status === 'pending' && approval.live !== false
    ) ||
    (questions ?? []).some(
      (question) => question.status === 'pending' && question.live === true
    )
  );
}

export function useHasPendingInteraction(): boolean {
  return useChainlitStore(hasPendingInteraction);
}

let confirmedNavigationTarget: string | undefined;

/**
 * Marks a thread navigation as already confirmed by the click guard, so
 * AutoResumeThread does not show the same warning twice for one navigation.
 */
export function confirmPendingNavigation(threadId: string): void {
  confirmedNavigationTarget = threadId;
}

/** Consumes the one-shot confirmation for a route-driven resume. */
export function consumeConfirmedNavigation(threadId: string): boolean {
  if (confirmedNavigationTarget !== threadId) return false;
  confirmedNavigationTarget = undefined;
  return true;
}
