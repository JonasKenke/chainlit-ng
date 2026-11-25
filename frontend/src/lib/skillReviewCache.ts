const STORAGE_KEY = 'chainlit:skill-reviews';

export interface SkillReviewFingerprint {
  id: string;
  contentHash?: string | null;
  version?: number;
  pinnedVersionId?: string | null;
}

interface CachedReview {
  reviewHash: string;
  contentHash?: string;
  version?: number;
  pinnedVersionId?: string | null;
}

function readReviews(): Record<string, CachedReview> {
  if (typeof window === 'undefined') return {};
  try {
    const parsed: unknown = JSON.parse(
      window.localStorage.getItem(STORAGE_KEY) ?? '{}'
    );
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {};
    }
    const reviews: Record<string, CachedReview> = {};
    for (const [skillId, value] of Object.entries(parsed)) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
      if (!('reviewHash' in value) || typeof value.reviewHash !== 'string') {
        continue;
      }
      if (!value.reviewHash) continue;
      const contentHash =
        'contentHash' in value && typeof value.contentHash === 'string'
          ? value.contentHash
          : undefined;
      const version =
        'version' in value && typeof value.version === 'number'
          ? value.version
          : undefined;
      const pinnedVersionId =
        'pinnedVersionId' in value && typeof value.pinnedVersionId === 'string'
          ? value.pinnedVersionId
          : null;
      reviews[skillId] = {
        reviewHash: value.reviewHash,
        contentHash,
        version,
        pinnedVersionId
      };
    }
    return reviews;
  } catch {
    return {};
  }
}

function sameFingerprint(
  cached: CachedReview,
  skill: SkillReviewFingerprint
): boolean {
  return (
    cached.contentHash === (skill.contentHash ?? undefined) &&
    cached.version === skill.version &&
    (cached.pinnedVersionId ?? null) === (skill.pinnedVersionId ?? null)
  );
}

/** Return the server review hash only while the local skill is unchanged. */
export function reviewedSkillHash(
  skill: SkillReviewFingerprint
): string | undefined {
  const cached = readReviews()[skill.id];
  return cached && sameFingerprint(cached, skill)
    ? cached.reviewHash
    : undefined;
}

/** Remember only the reviewed fingerprint; the warning gate stays server-side. */
export function rememberReviewedSkill(
  skill: SkillReviewFingerprint,
  reviewHash: string | null | undefined
): void {
  if (!reviewHash || typeof window === 'undefined') return;
  const reviews = readReviews();
  const next: CachedReview = {
    reviewHash,
    contentHash: skill.contentHash ?? undefined,
    version: skill.version,
    pinnedVersionId: skill.pinnedVersionId ?? null
  };
  const previous = reviews[skill.id];
  if (
    previous &&
    previous.reviewHash === next.reviewHash &&
    sameFingerprint(previous, skill)
  ) {
    return;
  }
  reviews[skill.id] = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(reviews));
  } catch {
    // Storage can be unavailable or full; the server still enforces the hash.
  }
}
