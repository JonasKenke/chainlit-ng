export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'edited'
  | 'expired'
  | 'superseded'
  | 'orphaned';

export type ApprovalDecision = 'approve' | 'reject' | 'edit' | null;

export interface IApproval {
  id: string;
  stepId: string;
  threadId: string;
  tool: string;
  title?: string | null;
  status: ApprovalStatus;
  requestedBy?: string | null;
  requestedAt?: string | null;
  expiresAt?: string | null;
  arguments?: Record<string, unknown>;
  argumentsPreview?: Record<string, unknown> | null;
  editedArgumentsPreview?: Record<string, unknown> | null;
  editAllowed?: boolean;
  decidedBy?: string | null;
  decidedAt?: string | null;
  decision?: ApprovalDecision;
  terminalReason?: string | null;
  revision?: number;
}

export interface IApprovalDecisionPayload {
  sessionId: string;
  approvalId: string;
  decision: 'approve' | 'reject' | 'edit';
  editedArguments?: Record<string, unknown>;
  decisionRequestId: string;
  reason?: string;
}

export interface IApprovalDecisionResponse {
  approvalId: string;
  status: ApprovalStatus;
  decision?: ApprovalDecision;
  decidedBy?: string | null;
  decidedAt?: string | null;
  terminalReason?: string | null;
  revision: number;
  error?: string | null;
}
