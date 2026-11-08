import { useContext, useMemo, useState } from 'react';
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
  edited: 'Edited',
  expired: 'Expired',
  superseded: 'Superseded',
  orphaned: 'Orphaned'
};

type Preview = Record<string, unknown> | null | undefined;

const flattenPreview = (
  value: unknown,
  prefix: string,
  leaves: Map<string, string>
): Map<string, string> => {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      flattenPreview(item, `${prefix}[${index}]`, leaves)
    );
    return leaves;
  }
  if (value && typeof value === 'object') {
    Object.entries(value as Record<string, unknown>).forEach(([key, item]) =>
      flattenPreview(item, prefix ? `${prefix}.${key}` : key, leaves)
    );
    return leaves;
  }
  leaves.set(prefix, JSON.stringify(value) ?? 'undefined');
  return leaves;
};

// Diff of redacted previews only: the full arguments never reach this view
// after a reload.
const diffPreviews = (before: Preview, after: Preview): string[] => {
  if (!after) return [];
  const left = flattenPreview(before ?? {}, '', new Map());
  const right = flattenPreview(after, '', new Map());
  const lines: string[] = [];
  new Set([...left.keys(), ...right.keys()]).forEach((path) => {
    const from = left.get(path);
    const to = right.get(path);
    if (from === to) return;
    if (from === undefined) lines.push(`+ ${path}: ${to}`);
    else if (to === undefined) lines.push(`- ${path}: ${from}`);
    else lines.push(`~ ${path}: ${from} → ${to}`);
  });
  return lines.sort();
};

interface Props {
  approval: IApproval;
}

export const ApprovalCard = ({ approval }: Props) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const setApprovals = useSetChainlitState('approvals');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const isPending = approval.status === 'pending';
  const diff = useMemo(
    () =>
      diffPreviews(approval.argumentsPreview, approval.editedArgumentsPreview),
    [approval.argumentsPreview, approval.editedArgumentsPreview]
  );

  const decide = async (
    decision: 'approve' | 'reject' | 'edit',
    editedArguments?: Record<string, unknown>
  ) => {
    setBusy(true);
    try {
      const response = await apiClient.respondToApproval({
        sessionId,
        approvalId: approval.id,
        decision,
        ...(editedArguments ? { editedArguments } : {}),
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

  const startEdit = () => {
    setDraft(JSON.stringify(approval.arguments ?? {}, null, 2));
    setEditing(true);
  };

  const submitEdit = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch {
      toast.error('Arguments must be valid JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      toast.error('Arguments must be a JSON object');
      return;
    }
    setEditing(false);
    decide('edit', parsed as Record<string, unknown>);
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

      {diff.length ? (
        <div data-testid="approval-diff" className="mt-3">
          <div className="text-xs font-medium text-muted-foreground">
            Original → edited
          </div>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-muted p-3 text-xs">
            {diff.join('\n')}
          </pre>
        </div>
      ) : null}

      {editing ? (
        <div className="mt-3">
          <textarea
            aria-label="Edited arguments JSON"
            className="h-40 w-full rounded border border-border bg-background p-2 font-mono text-xs"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={busy}
              onClick={submitEdit}
            >
              Save edit
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : isPending ? (
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
          {approval.editAllowed ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={startEdit}
            >
              Edit
            </Button>
          ) : null}
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
