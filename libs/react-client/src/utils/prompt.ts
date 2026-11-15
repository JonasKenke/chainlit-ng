import { IPromptUpdate, IPromptVersionSummary } from '../types/prompt';

export function mergePromptUpdates(
  previous: IPromptUpdate[],
  next: IPromptUpdate[]
): IPromptUpdate[] {
  const byPrompt = new Map<string, IPromptUpdate>();
  for (const update of previous) {
    byPrompt.set(update.promptId, update);
  }
  for (const update of next) {
    const existing = byPrompt.get(update.promptId);
    if (!existing || update.latestVersion >= existing.latestVersion) {
      byPrompt.set(update.promptId, update);
    }
  }
  return [...byPrompt.values()].sort(
    (a, b) => b.latestVersion - a.latestVersion
  );
}

/**
 * Extract the stable error code from a `ClientError` raised by the prompt
 * versioning routes (`409`/`413` bodies carry `{code, message}` in `detail`).
 */
export function promptErrorCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('detail' in error)) {
    return undefined;
  }
  const detail = error.detail;
  if (!detail || typeof detail !== 'object' || !('code' in detail)) {
    return undefined;
  }
  return typeof detail.code === 'string' ? detail.code : undefined;
}

/** Human-readable one-line summary of a version snapshot. */
export function promptVersionLabel(version: IPromptVersionSummary): string {
  return `v${version.version}`;
}
