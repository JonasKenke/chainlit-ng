export type ApprovalStatus =
  'pending' | 'approved' | 'rejected' | 'expired' | 'superseded' | 'orphaned';

export type ApprovalDecision = 'approve' | 'reject' | null;

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
  decidedBy?: string | null;
  decidedAt?: string | null;
  decision?: ApprovalDecision;
  terminalReason?: string | null;
  revision?: number;
}

export interface IApprovalDecisionPayload {
  sessionId: string;
  approvalId: string;
  decision: 'approve' | 'reject';
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
