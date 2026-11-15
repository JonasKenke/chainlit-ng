export interface ISkill {
  id: string;
  title: string;
  content: string;
  userId: string;
  isShared: boolean;
  createdAt: string;
  updatedAt: string;
  kind?: 'skill';
  description?: string;
  enabled?: boolean;
  importedFrom?: string | null;
  importedAt?: string | null;
  contentHash?: string | null;
  /** Head version number. */
  version?: number;
  /** Version id of the head snapshot, when versioning is available. */
  currentVersionId?: string | null;
  /** Shared source this skill is pinned to, if it was copied from a link. */
  sourcePromptId?: string | null;
  pinnedVersionId?: string | null;
  /** Source version number when this row is a pinned copy. */
  pinnedVersion?: number | null;
  pinnedContentHash?: string | null;
  pinnedAt?: string | null;
  /** Source version acknowledged for update notices. */
  updateSeenVersionId?: string | null;
}

export interface ISkillPayload {
  name: string;
  description: string;
  content: string;
  enabled?: boolean;
}

export interface ISkillUpdate {
  name?: string;
  description?: string;
  content?: string;
  enabled?: boolean;
  /**
   * Fingerprint of the version being enabled. Imported and pinned skills
   * require it so a body cannot reach a model without a fresh review.
   */
  reviewedContentHash?: string;
}

export interface ISkillExport {
  markdown: string;
  filename: string;
  contentHash?: string;
}

/**
 * The anonymous public projection of a shared skill: never contains `userId`,
 * `enabled`, import provenance or any other owner identity.
 */
export interface ISharedSkill {
  id: string;
  title: string;
  description: string;
  content: string;
  version: number;
  currentVersionId: string | null;
  contentHash: string | null;
  updatedAt: string;
  sharedAt?: string | null;
  /** Fixed, non-identifying author label. */
  authorLabel: string;
}

export interface ISkillShareResult {
  skill: ISkill;
  shareUrl: string;
}

/** A pinned skill copy whose shared source is ahead of the acknowledged version. */
export interface ISkillUpdateNotice {
  skillId: string;
  title: string;
  sourcePromptId: string;
  pinnedVersionId: string | null;
  pinnedVersion: number | null;
  localVersion: number;
  latestVersionId: string;
  latestVersion: number;
  seenVersionId?: string | null;
  seenVersion?: number | null;
}

export interface ISkillVersionSummary {
  id: string;
  promptId: string;
  kind: 'skill';
  version: number;
  title: string;
  description: string;
  contentHash: string;
  createdAt: string;
  sourceVersionId?: string | null;
  /** Only present for owners; anonymous share routes never include it. */
  createdBy?: string;
}

export interface ISkillVersion extends ISkillVersionSummary {
  content: string;
}

export interface ISkillSeenAck {
  skillId: string;
  version: number;
  updateSeenVersionId?: string | null;
}

export interface SkillUpdatePayload {
  versionId?: string;
  confirm?: boolean;
  expectedLocalHash?: string | null;
}

export interface SkillVersionQuery {
  /** Use the anonymous shared-skill routes instead of the owned routes. */
  shared?: boolean;
}

/**
 * A presentation-only diff row computed in the browser. Skills do not expose
 * a server diff route, so the update dialog derives rows from version bodies.
 */
export type SkillDiffOp = 'equal' | 'insert' | 'delete' | 'replace';

export interface ISkillDiffRow {
  op: SkillDiffOp;
  oldLine: number | null;
  newLine: number | null;
  oldText: string | null;
  newText: string | null;
}

export interface ISkillDiff {
  rows: ISkillDiffRow[];
  truncated: boolean;
}
