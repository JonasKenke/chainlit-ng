export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'edited'
  | 'changes_requested'
  | 'expired'
  | 'superseded'
  | 'orphaned';

export type ApprovalDecision =
  'approve' | 'reject' | 'edit' | 'request_changes' | null;

export type ApprovalEditFieldKind =
  'string' | 'text' | 'integer' | 'number' | 'boolean' | 'enum' | 'string_list';

export interface IApprovalEditField {
  name: string;
  label: string;
  kind: ApprovalEditFieldKind;
  required: boolean;
  nullable: boolean;
  secret: boolean;
  serverHeld?: boolean;
  options?: (string | number | boolean)[] | null;
  description?: string | null;
  default?: unknown;
}

export interface IApproval {
  id: string;
  stepId: string;
  threadId: string;
  tool: string;
  title?: string | null;
  content?: string | null;
  status: ApprovalStatus;
  requestedBy?: string | null;
  requestedAt?: string | null;
  expiresAt?: string | null;
  arguments?: Record<string, unknown>;
  argumentsPreview?: Record<string, unknown> | null;
  editedArgumentsPreview?: Record<string, unknown> | null;
  editAllowed?: boolean;
  requestChangesAllowed?: boolean;
  editFields?: IApprovalEditField[] | null;
  live?: boolean;
  reason?: string | null;
  decidedBy?: string | null;
  decidedAt?: string | null;
  decision?: ApprovalDecision;
  terminalReason?: string | null;
  revision?: number;
}

export interface IApprovalDecisionPayload {
  sessionId: string;
  approvalId: string;
  decision: 'approve' | 'reject' | 'edit' | 'request_changes';
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
  reason?: string | null;
  revision: number;
  error?: string | null;
}
