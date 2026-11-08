import { useContext, useState } from 'react';
import { toast } from 'sonner';
import { v4 as uuidv4 } from 'uuid';

import {
  ChainlitContext,
  IApproval,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit/react-client';

import { Button } from '@/components/ui/button';

const statusLabel: Record<IApproval['status'], string> = {
  pending: 'Pending approval',
  approved: 'Approved',
  rejected: 'Rejected',
  expired: 'Expired',
  superseded: 'Superseded',
  orphaned: 'Orphaned'
};

interface Props {
  approval: IApproval;
}

export const ApprovalCard = ({ approval }: Props) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const setApprovals = useSetChainlitState('approvals');
  const [busy, setBusy] = useState(false);

  const isPending = approval.status === 'pending';

  const decide = async (decision: 'approve' | 'reject') => {
    setBusy(true);
    try {
      const response = await apiClient.respondToApproval({
        sessionId,
        approvalId: approval.id,
        decision,
        decisionRequestId: uuidv4()
      });
      setApprovals((old) =>
        old.map((item) =>
          item.id === response.approvalId
            ? {
                ...item,
                status: response.status,
                decision: response.decision,
                decidedBy: response.decidedBy,
                decidedAt: response.decidedAt,
                terminalReason: response.terminalReason,
                revision: response.revision
              }
            : item
        )
      );
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : 'Unable to decide approval'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      data-testid="approval-card"
      aria-label={`Approval for ${approval.tool}`}
      className="rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-semibold">{approval.tool}</div>
          {approval.title ? (
            <div className="text-sm text-muted-foreground">
              {approval.title}
            </div>
          ) : null}
        </div>
        <span className="text-xs font-medium uppercase text-muted-foreground">
          {statusLabel[approval.status]}
        </span>
      </div>

      <pre className="mt-3 max-h-64 overflow-auto rounded bg-muted p-3 text-xs">
        {JSON.stringify(approval.arguments ?? {}, null, 2)}
      </pre>

      {isPending ? (
        <div className="mt-3 flex gap-2">
          <Button
            type="button"
            size="sm"
            disabled={busy}
            onClick={() => decide('approve')}
          >
            Approve
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => decide('reject')}
          >
            Reject
          </Button>
        </div>
      ) : (
        <div className="mt-3 text-xs text-muted-foreground">
          {approval.decidedBy ? `By ${approval.decidedBy}` : null}
          {approval.decidedBy && approval.decidedAt ? ' · ' : null}
          {approval.decidedAt ?? approval.terminalReason ?? null}
        </div>
      )}
    </section>
  );
};
