import { ISkillUpdateNotice } from '../types/skill';

/**
 * Merge refresh results with the notices already held in the store. A notice
 * acknowledged this session survives a response that no longer lists it, so
 * the quiet row marker persists until the copy is updated, detached or
 * deleted. A newer server notice always wins.
 */
export function mergeSkillUpdates(
  previous: ISkillUpdateNotice[],
  next: ISkillUpdateNotice[]
): ISkillUpdateNotice[] {
  const bySkill = new Map<string, ISkillUpdateNotice>();
  for (const update of previous) {
    bySkill.set(update.skillId, update);
  }
  for (const update of next) {
    const existing = bySkill.get(update.skillId);
    if (!existing || update.latestVersion >= existing.latestVersion) {
      bySkill.set(update.skillId, update);
    }
  }
  return [...bySkill.values()].sort(
    (a, b) => b.latestVersion - a.latestVersion
  );
}
