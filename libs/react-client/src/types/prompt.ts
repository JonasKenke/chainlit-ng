/** A gallery prompt owned by the current user (or a pinned copy). */
export interface IPrompt {
  id: string;
  title: string;
  content: string;
  userId: string;
  isShared: boolean;
  createdAt: string;
  updatedAt: string;
  kind?: 'prompt' | 'skill';
  description?: string;
  /** Head version number. */
  version?: number;
  /** Fingerprint of the head content (`sha256:...`). */
  contentHash?: string | null;
  /** Version id of the head snapshot, when versioning is available. */
  currentVersionId?: string | null;
  /** Source prompt this copy is pinned to. */
  sourcePromptId?: string | null;
  pinnedVersionId?: string | null;
  /** Source version number when this row is a pinned copy. */
  pinnedVersion?: number | null;
  pinnedContentHash?: string | null;
  pinnedAt?: string | null;
}

/**
 * The anonymous public projection of a shared prompt: never contains
 * `userId`, `createdBy` or any other owner identity.
 */
export interface ISharedPrompt {
  id: string;
  title: string;
  content: string;
  isShared: boolean;
  createdAt: string;
  updatedAt: string;
  version: number;
  currentVersionId: string | null;
  currentContentHash: string | null;
}

export interface IPromptVersionSummary {
  id: string;
  promptId: string;
  kind: 'prompt' | 'skill';
  version: number;
  title: string;
  description: string;
  contentHash: string;
  createdAt: string;
  sourceVersionId?: string | null;
  /** Only present for owners; anonymous share routes never include it. */
  createdBy?: string;
}

export interface IPromptVersion extends IPromptVersionSummary {
  content: string;
}

/** A pinned copy whose shared source is ahead of the acknowledged version. */
export interface IPromptUpdate {
  promptId: string;
  title: string;
  sourcePromptId: string;
  pinnedVersionId: string | null;
  pinnedVersion: number | null;
  localVersion: number;
  latestVersionId: string;
  latestVersion: number;
  notifiedVersion: number;
}

export interface IPromptUpdateAck {
  promptId: string;
  notifiedVersion: number;
}

export type PromptDiffOp = 'equal' | 'insert' | 'delete' | 'replace';

/** One presentation-neutral diff row; text is never HTML. */
export interface IPromptDiffRow {
  op: PromptDiffOp;
  oldLine: number | null;
  newLine: number | null;
  oldText: string | null;
  newText: string | null;
}

export interface IPromptDiff {
  promptId: string;
  from: 'pinned' | 'local';
  to: string;
  fromVersionId: string | null;
  toVersionId: string;
  fromVersion: number | null;
  toVersion: number;
  fromContentHash: string | null;
  toContentHash: string;
  rows: IPromptDiffRow[];
  truncated: boolean;
  maxRows: number;
  maxBytes: number;
}

export type PromptSyncErrorCode =
  | 'local_changes'
  | 'preview_stale'
  | 'source_unavailable'
  | 'already_current'
  | 'version_conflict'
  | 'not_pinned'
  | 'pinned_unavailable'
  | 'already_added';

export interface PromptVersionQuery {
  /** Use the anonymous shared-prompt routes instead of the owned routes. */
  shared?: boolean;
}

export interface PromptSyncPayload {
  versionId?: string;
  confirm?: boolean;
  expectedLocalHash?: string | null;
}
