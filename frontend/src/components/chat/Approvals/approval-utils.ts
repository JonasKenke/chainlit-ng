import type { IApproval } from '@chainlit-ng/react-client';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  MessageSquare,
  MinusCircle,
  XCircle
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export const approvalDecisionKey = (
  status: IApproval['status']
): string | null => {
  switch (status) {
    case 'approved':
      return 'chat.approval.status.approved';
    case 'edited':
      return 'chat.approval.status.edited';
    case 'rejected':
      return 'chat.approval.status.rejected';
    case 'changes_requested':
      return 'chat.approval.status.changesRequested';
    case 'expired':
      return 'chat.approval.status.expired';
    case 'superseded':
      return 'chat.approval.status.superseded';
    case 'orphaned':
      return 'chat.approval.status.orphaned';
    default:
      return null;
  }
};

export const approvalDecisionFallback = (
  status: IApproval['status']
): string => {
  switch (status) {
    case 'approved':
      return 'Approved';
    case 'edited':
      return 'Approved with edits';
    case 'rejected':
      return 'Rejected';
    case 'changes_requested':
      return 'Changes requested';
    case 'expired':
      return 'Expired';
    case 'superseded':
      return 'Superseded';
    default:
      return 'Unavailable';
  }
};

export const approvalDecisionMeta = (
  status: IApproval['status']
): { icon: LucideIcon; tone: string } => {
  switch (status) {
    case 'approved':
    case 'edited':
      return {
        icon: CheckCircle2,
        tone: 'text-emerald-600 dark:text-emerald-400'
      };
    case 'rejected':
      return { icon: XCircle, tone: 'text-red-600 dark:text-red-400' };
    case 'changes_requested':
      return {
        icon: MessageSquare,
        tone: 'text-amber-600 dark:text-amber-400'
      };
    case 'expired':
      return { icon: Clock, tone: 'text-muted-foreground' };
    case 'superseded':
      return { icon: MinusCircle, tone: 'text-muted-foreground' };
    default:
      return { icon: AlertTriangle, tone: 'text-muted-foreground' };
  }
};

export const approvalErrorMessage = (
  error: unknown,
  fallback: string
): string => {
  if (typeof error === 'object' && error !== null && 'payload' in error) {
    const payload = error.payload;
    if (
      typeof payload === 'object' &&
      payload !== null &&
      'detail' in payload
    ) {
      const detail = payload.detail;
      if (typeof detail === 'string' && detail) return detail;
      if (typeof detail === 'object' && detail !== null) {
        // 422 ApprovalInvalidArguments carries the schema field path and the
        // Pydantic error type; never the submitted value.
        const field = 'field' in detail ? detail.field : undefined;
        const type = 'type' in detail ? detail.type : undefined;
        if (typeof field === 'string') {
          return typeof type === 'string' ? `${field} (${type})` : field;
        }
      }
    }
  }
  return error instanceof Error ? error.message : fallback;
};
