import { computeTextDiff } from '@/lib/skillDiff';
import {
  ISkill,
  ISkillVersion,
  ISkillVersionSummary,
  promptErrorCode,
  useSkills
} from '@chainlit-ng/react-client';
import { AlertTriangle, GitCompare, ShieldAlert } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import {
  VersionDiffRows,
  VersionDiffViewMode
} from '@/components/VersionDiffRows';
import { VersionPicker } from '@/components/VersionPicker';
import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';

export interface SkillUpdateDialogProps {
  /** The local pinned skill to compare and update. */
  skill: ISkill | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Preselected source version (e.g. the version offered by a notice). */
  initialVersionId?: string | null;
  /** Called with the updated skill after a successful update or detach. */
  onUpdated?: (skill: ISkill) => void;
}

/**
 * Review a shared skill version before copying it over the pinned local copy.
 * The server always stores the result disabled, so this dialog never enables a
 * skill; the review dialog is the only path that does.
 */
export function SkillUpdateDialog({
  skill,
  open,
  onOpenChange,
  initialVersionId,
  onUpdated
}: SkillUpdateDialogProps) {
  const { t } = useTranslation();
  const {
    fetchSkillVersions,
    fetchSkillVersion,
    updateSkillFromSource,
    detachSkill
  } = useSkills();

  const [versions, setVersions] = useState<ISkillVersionSummary[]>([]);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [target, setTarget] = useState<ISkillVersion | null>(null);
  const [viewMode, setViewMode] = useState<VersionDiffViewMode>('unified');
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [loadingTarget, setLoadingTarget] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [confirmLocal, setConfirmLocal] = useState(false);
  const [stale, setStale] = useState(false);
  const [sourceUnavailable, setSourceUnavailable] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const [detaching, setDetaching] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  const skillId = skill?.id;
  const sourceId = skill?.sourcePromptId ?? null;
  const isPinned = Boolean(sourceId);
  const versionSourceId = sourceId ?? skillId;

  const loadVersions = useCallback(async () => {
    if (!versionSourceId) return;
    setLoadingVersions(true);
    setLoadError(false);
    try {
      const list = await fetchSkillVersions(versionSourceId, {
        shared: isPinned
      });
      const sorted = [...list]
        .filter(
          (version) =>
            !skill?.contentHash || version.contentHash !== skill.contentHash
        )
        .sort((a, b) => b.version - a.version);
      setVersions(sorted);
      const preferred = sorted.some(
        (version) => version.id === initialVersionId
      )
        ? initialVersionId
        : sorted[0]?.id;
      setTargetId(preferred ?? null);
    } catch {
      setVersions([]);
      setLoadError(true);
    } finally {
      setLoadingVersions(false);
    }
  }, [
    versionSourceId,
    isPinned,
    skill?.contentHash,
    fetchSkillVersions,
    initialVersionId
  ]);

  useEffect(() => {
    if (!open || !skillId || !versionSourceId) return;
    setConfirmLocal(false);
    setStale(false);
    setSourceUnavailable(false);
    setActionError(null);
    void loadVersions();
  }, [open, skillId, versionSourceId, initialVersionId, loadVersions]);

  useEffect(() => {
    if (!open || !versionSourceId || !targetId) {
      setTarget(null);
      return;
    }
    let cancelled = false;
    setLoadingTarget(true);
    setConfirmLocal(false);
    setStale(false);
    setActionError(null);
    fetchSkillVersion(versionSourceId, targetId, { shared: isPinned })
      .then((version) => {
        if (!cancelled) setTarget(version);
      })
      .catch((error) => {
        if (cancelled) return;
        const code = promptErrorCode(error);
        if (
          isPinned &&
          (code === 'source_unavailable' || code === 'not_pinned')
        ) {
          setSourceUnavailable(true);
        } else {
          setLoadError(true);
        }
        setTarget(null);
      })
      .finally(() => {
        if (!cancelled) setLoadingTarget(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    open,
    versionSourceId,
    isPinned,
    targetId,
    fetchSkillVersion,
    reloadToken
  ]);

  const targetVersion = useMemo(
    () => versions.find((version) => version.id === targetId) ?? null,
    [versions, targetId]
  );

  const diff = useMemo(
    () =>
      skill && target
        ? computeTextDiff(skill.content, target.content)
        : { rows: [], truncated: false },
    [skill, target]
  );

  // The pinned snapshot is the last reviewed source body; a mismatch means the
  // local copy was edited since it was added.
  const locallyModified = Boolean(
    skill?.pinnedContentHash &&
    skill.contentHash &&
    skill.pinnedContentHash !== skill.contentHash
  );
  const alreadyCurrent = Boolean(
    target && skill?.contentHash && target.contentHash === skill.contentHash
  );

  const handleUpdate = async (confirm: boolean) => {
    if (!skillId) return;
    setUpdating(true);
    setActionError(null);
    try {
      const updated = await updateSkillFromSource(skillId, {
        versionId: targetId ?? undefined,
        confirm,
        expectedLocalHash: skill?.contentHash ?? undefined
      });
      toast.success(t('chat.skills.update.updated'));
      onUpdated?.(updated);
      onOpenChange(false);
    } catch (error) {
      const code = promptErrorCode(error);
      if (code === 'local_changes') {
        setConfirmLocal(true);
      } else if (code === 'preview_stale') {
        setStale(true);
        setConfirmLocal(false);
        setReloadToken((value) => value + 1);
        await loadVersions();
      } else if (code === 'source_unavailable') {
        setSourceUnavailable(true);
      } else if (code === 'version_conflict') {
        setActionError(t('chat.skills.update.versionConflict'));
      } else {
        setActionError(t('chat.skills.update.error'));
      }
    } finally {
      setUpdating(false);
    }
  };

  const handleDetach = async () => {
    if (!skillId) return;
    setDetaching(true);
    setActionError(null);
    try {
      const updated = await detachSkill(skillId);
      toast.success(t('chat.skills.update.detached'));
      onUpdated?.(updated);
      onOpenChange(false);
    } catch {
      setActionError(t('chat.skills.update.error'));
    } finally {
      setDetaching(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-3xl flex flex-col max-h-[85vh] gap-3"
        data-testid="skill-update-dialog"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GitCompare className="h-4 w-4" />
            {t(
              isPinned
                ? 'chat.skills.update.title'
                : 'chat.skills.update.historyTitle'
            )}
          </DialogTitle>
          <DialogDescription>
            {t(
              isPinned
                ? 'chat.skills.update.description'
                : 'chat.skills.update.historyDescription'
            )}
          </DialogDescription>
        </DialogHeader>

        {loadingVersions ? (
          <p className="text-sm text-muted-foreground">
            {t('common.status.loading')}
          </p>
        ) : loadError ? (
          <p className="text-sm text-destructive">
            {t('chat.skills.update.loadError')}
          </p>
        ) : versions.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t(
              isPinned
                ? 'chat.skills.update.upToDate'
                : 'chat.skills.update.noEarlierVersions'
            )}
          </p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2">
              <VersionPicker
                value={targetId ?? ''}
                options={versions}
                onChange={setTargetId}
                label={t('chat.skills.update.version')}
                latestId={versions[0]?.id}
                testId="skill-update-version"
              />
              <div className="flex items-center gap-1" role="group">
                <Button
                  type="button"
                  size="sm"
                  variant={viewMode === 'unified' ? 'default' : 'outline'}
                  aria-pressed={viewMode === 'unified'}
                  onClick={() => setViewMode('unified')}
                  data-testid="skill-update-mode-unified"
                >
                  {t('chat.skills.update.unified')}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={viewMode === 'split' ? 'default' : 'outline'}
                  aria-pressed={viewMode === 'split'}
                  onClick={() => setViewMode('split')}
                  data-testid="skill-update-mode-split"
                >
                  {t('chat.skills.update.split')}
                </Button>
              </div>
            </div>

            {sourceUnavailable ? (
              <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
                <p className="flex items-center gap-2 text-destructive">
                  <ShieldAlert className="h-4 w-4" />
                  {t('chat.skills.update.sourceUnavailable')}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t('chat.skills.update.sourceUnavailableHint')}
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="self-start"
                  onClick={handleDetach}
                  disabled={detaching}
                  data-testid="skill-update-detach"
                >
                  {t('chat.skills.update.detach')}
                </Button>
              </div>
            ) : null}

            {stale ? (
              <p
                className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400"
                data-testid="skill-update-stale"
              >
                <AlertTriangle className="h-4 w-4" />
                {t('chat.skills.update.stale')}
              </p>
            ) : null}

            {locallyModified && !confirmLocal ? (
              <p
                className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400"
                data-testid="skill-update-local-warning"
              >
                <AlertTriangle className="h-4 w-4" />
                {t('chat.skills.update.localChanges')}
              </p>
            ) : null}

            {confirmLocal ? (
              <div className="flex flex-col gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                <p className="flex items-center gap-2 font-medium">
                  <AlertTriangle className="h-4 w-4" />
                  {t('chat.skills.update.localChanges')}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t('chat.skills.update.localChangesHint')}
                </p>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => handleUpdate(true)}
                    disabled={updating}
                    data-testid="skill-update-confirm"
                  >
                    {t('chat.skills.update.confirmUpdate')}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setConfirmLocal(false)}
                    disabled={updating}
                  >
                    {t('common.actions.cancel')}
                  </Button>
                </div>
              </div>
            ) : null}

            {alreadyCurrent ? (
              <p
                className="text-xs text-muted-foreground"
                data-testid="skill-update-already-current"
              >
                {t('chat.skills.update.alreadyCurrent')}
              </p>
            ) : null}

            {actionError ? (
              <p className="text-sm text-destructive" role="alert">
                {actionError}
              </p>
            ) : null}

            <div className="flex-1 overflow-auto rounded-md border custom-scrollbar">
              {loadingTarget ? (
                <p className="p-3 text-sm text-muted-foreground">
                  {t('common.status.loading')}
                </p>
              ) : diff.rows.length === 0 && target ? (
                <p className="p-3 text-sm text-muted-foreground">
                  {t('chat.skills.update.noChanges')}
                </p>
              ) : (
                <VersionDiffRows
                  rows={diff.rows}
                  viewMode={viewMode}
                  testId="skill-update-row"
                />
              )}
            </div>

            {diff.truncated ? (
              <p className="text-xs text-muted-foreground">
                {t('chat.skills.update.truncated')}
              </p>
            ) : null}

            {isPinned ? (
              <div className="flex items-center justify-end">
                <Button
                  type="button"
                  onClick={() => handleUpdate(false)}
                  disabled={
                    !targetId ||
                    updating ||
                    alreadyCurrent ||
                    sourceUnavailable ||
                    confirmLocal
                  }
                  data-testid="skill-update-submit"
                >
                  {targetVersion
                    ? t('chat.skills.update.update', {
                        version: targetVersion.version
                      })
                    : t('chat.skills.update.updateFallback')}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default SkillUpdateDialog;
