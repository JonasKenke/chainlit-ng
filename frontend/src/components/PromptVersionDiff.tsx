import { computeTextDiff } from '@/lib/skillDiff';
import {
  IPrompt,
  IPromptDiffRow,
  IPromptVersionSummary,
  promptErrorCode,
  usePromptGallery
} from '@chainlit-ng/react-client';
import { AlertTriangle, GitCompare, ShieldAlert } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import {
  VersionDiffRows,
  VersionDiffViewMode
} from '@/components/VersionDiffRows';
import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';

export interface PromptVersionDiffProps {
  /** The local pinned copy to compare and update. */
  prompt: IPrompt | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Preselected target version (e.g. the version offered by a notice). */
  initialVersionId?: string | null;
  /** Called with the updated prompt after a successful sync or detach. */
  onUpdated?: (prompt: IPrompt) => void;
}

type ViewMode = VersionDiffViewMode;

function formatDate(value?: string): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString();
}

export function PromptVersionDiff({
  prompt,
  open,
  onOpenChange,
  initialVersionId,
  onUpdated
}: PromptVersionDiffProps) {
  const { t } = useTranslation();
  const { fetchPromptVersions, fetchPromptVersion, syncPrompt, detachPrompt } =
    usePromptGallery();

  const [versions, setVersions] = useState<IPromptVersionSummary[]>([]);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [diff, setDiff] = useState<{
    rows: IPromptDiffRow[];
    truncated: boolean;
  } | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('unified');
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [loadingDiff, setLoadingDiff] = useState(false);
  const [versionsError, setVersionsError] = useState(false);
  const [diffError, setDiffError] = useState(false);
  const [confirmLocal, setConfirmLocal] = useState(false);
  const [stale, setStale] = useState(false);
  const [sourceUnavailable, setSourceUnavailable] = useState(false);
  const [alreadyCurrent, setAlreadyCurrent] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [detaching, setDetaching] = useState(false);

  const promptId = prompt?.id;
  const isPinned = Boolean(prompt?.sourcePromptId);
  const versionSourceId = isPinned
    ? (prompt?.sourcePromptId ?? null)
    : promptId;

  const loadDiff = useCallback(async () => {
    if (!versionSourceId || !targetId) {
      setDiff(null);
      return;
    }
    setLoadingDiff(true);
    setDiffError(false);
    try {
      const target = await fetchPromptVersion(versionSourceId, targetId, {
        shared: isPinned
      });
      setDiff(computeTextDiff(prompt?.content ?? '', target.content));
    } catch (error) {
      const code = promptErrorCode(error);
      if (
        isPinned &&
        (code === 'pinned_unavailable' ||
          code === 'not_pinned' ||
          code === 'source_unavailable')
      ) {
        setSourceUnavailable(true);
        setDiff(null);
      } else {
        setDiffError(true);
      }
    } finally {
      setLoadingDiff(false);
    }
  }, [
    versionSourceId,
    targetId,
    prompt?.content,
    isPinned,
    fetchPromptVersion
  ]);

  const loadVersions = useCallback(async () => {
    if (!versionSourceId) return;
    setLoadingVersions(true);
    setVersionsError(false);
    try {
      const list = await fetchPromptVersions(versionSourceId, {
        shared: isPinned
      });
      const sorted = [...list].sort((a, b) => b.version - a.version);
      setVersions(sorted);
      const preferred = sorted.some(
        (version) => version.id === initialVersionId
      )
        ? initialVersionId
        : isPinned
          ? sorted[0]?.id
          : (sorted[1]?.id ?? sorted[0]?.id);
      setTargetId(preferred ?? null);
    } catch (error) {
      const code = promptErrorCode(error);
      if (
        isPinned &&
        (code === 'pinned_unavailable' ||
          code === 'not_pinned' ||
          code === 'source_unavailable')
      ) {
        setSourceUnavailable(true);
      }
      setVersions([]);
      setVersionsError(true);
    } finally {
      setLoadingVersions(false);
    }
  }, [versionSourceId, isPinned, fetchPromptVersions, initialVersionId]);

  useEffect(() => {
    if (!open || !promptId) return;
    setConfirmLocal(false);
    setStale(false);
    setSourceUnavailable(false);
    setAlreadyCurrent(false);
    setActionError(null);
    void loadVersions();
  }, [open, versionSourceId, initialVersionId, loadVersions]);

  useEffect(() => {
    if (!open || !targetId) return;
    setConfirmLocal(false);
    setStale(false);
    setAlreadyCurrent(false);
    setActionError(null);
    void loadDiff();
  }, [open, targetId, loadDiff]);

  const targetVersion = useMemo(
    () => versions.find((version) => version.id === targetId) ?? null,
    [versions, targetId]
  );

  const handleUpdate = async (confirm: boolean) => {
    if (!promptId) return;
    setSyncing(true);
    setActionError(null);
    try {
      const updated = await syncPrompt(promptId, {
        versionId: targetId ?? undefined,
        confirm,
        expectedLocalHash: prompt?.contentHash ?? undefined
      });
      toast.success(t('chat.promptGallery.diff.updated'));
      onUpdated?.(updated);
      onOpenChange(false);
    } catch (error) {
      const code = promptErrorCode(error);
      if (code === 'local_changes') {
        setConfirmLocal(true);
      } else if (code === 'preview_stale') {
        setStale(true);
        setConfirmLocal(false);
        await loadDiff();
        await loadVersions();
      } else if (code === 'source_unavailable') {
        setSourceUnavailable(true);
      } else if (code === 'already_current') {
        setAlreadyCurrent(true);
      } else {
        setActionError(t('chat.promptGallery.diff.updateError'));
      }
    } finally {
      setSyncing(false);
    }
  };

  const handleDetach = async () => {
    if (!promptId) return;
    setDetaching(true);
    setActionError(null);
    try {
      const updated = await detachPrompt(promptId);
      toast.success(t('chat.promptGallery.diff.detached'));
      onUpdated?.(updated);
      onOpenChange(false);
    } catch {
      setActionError(t('chat.promptGallery.diff.updateError'));
    } finally {
      setDetaching(false);
    }
  };

  const rows = diff?.rows ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-3xl flex flex-col max-h-[85vh] gap-3"
        data-testid="prompt-diff-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GitCompare className="h-4 w-4" />
            {t(
              isPinned
                ? 'chat.promptGallery.diff.title'
                : 'chat.promptGallery.diff.historyTitle'
            )}
          </DialogTitle>
          <DialogDescription>
            {t(
              isPinned
                ? 'chat.promptGallery.diff.description'
                : 'chat.promptGallery.diff.historyDescription'
            )}
          </DialogDescription>
        </DialogHeader>

        {sourceUnavailable ? (
          <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <p className="flex items-center gap-2 text-destructive">
              <ShieldAlert className="h-4 w-4" />
              {t('chat.promptGallery.diff.sourceUnavailable')}
            </p>
            <p className="text-xs text-muted-foreground">
              {t('chat.promptGallery.diff.sourceUnavailableHint')}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="self-start"
              onClick={handleDetach}
              disabled={detaching}
              data-testid="prompt-diff-detach"
            >
              {t('chat.promptGallery.diff.detach')}
            </Button>
          </div>
        ) : loadingVersions ? (
          <p className="text-sm text-muted-foreground">
            {t('common.status.loading')}
          </p>
        ) : versionsError ? (
          <p className="text-sm text-destructive">
            {t('chat.promptGallery.diff.loadError')}
          </p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2">
              <select
                id="prompt-diff-version"
                data-testid="prompt-diff-version"
                aria-label={t('chat.promptGallery.diff.version')}
                value={targetId ?? ''}
                onChange={(event) => setTargetId(event.target.value)}
                className="h-8 min-w-20 rounded-md border bg-background px-2 text-xs"
              >
                {versions.map((version) => (
                  <option key={version.id} value={version.id}>
                    {`v${version.version}`}
                  </option>
                ))}
              </select>
              <div className="flex items-center gap-1" role="group">
                <Button
                  type="button"
                  size="sm"
                  variant={viewMode === 'unified' ? 'default' : 'outline'}
                  aria-pressed={viewMode === 'unified'}
                  onClick={() => setViewMode('unified')}
                  data-testid="prompt-diff-mode-unified"
                >
                  {t('chat.promptGallery.diff.unified')}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={viewMode === 'split' ? 'default' : 'outline'}
                  aria-pressed={viewMode === 'split'}
                  onClick={() => setViewMode('split')}
                  data-testid="prompt-diff-mode-split"
                >
                  {t('chat.promptGallery.diff.split')}
                </Button>
              </div>
            </div>

            {targetVersion ? (
              <p
                className="text-xs text-muted-foreground"
                data-testid="prompt-diff-metadata"
                title={[targetVersion.contentHash, targetVersion.createdBy]
                  .filter(Boolean)
                  .join(' · ')}
              >
                <span className="text-foreground">
                  v{targetVersion.version}
                </span>
                {' · '}
                <time dateTime={targetVersion.createdAt}>
                  {formatDate(targetVersion.createdAt)}
                </time>
              </p>
            ) : null}

            {stale ? (
              <p
                className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400"
                data-testid="prompt-diff-stale"
              >
                <AlertTriangle className="h-4 w-4" />
                {t('chat.promptGallery.diff.stale')}
              </p>
            ) : null}

            {confirmLocal ? (
              <div
                className="flex flex-col gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
                data-testid="prompt-diff-local-warning"
              >
                <p className="flex items-center gap-2 font-medium">
                  <AlertTriangle className="h-4 w-4" />
                  {t('chat.promptGallery.diff.localChanges')}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t('chat.promptGallery.diff.localChangesHint')}
                </p>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => handleUpdate(true)}
                    disabled={syncing}
                    data-testid="prompt-diff-confirm"
                  >
                    {t('chat.promptGallery.diff.confirmUpdate')}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setConfirmLocal(false)}
                    disabled={syncing}
                  >
                    {t('common.actions.cancel')}
                  </Button>
                </div>
              </div>
            ) : null}

            {alreadyCurrent ? (
              <p
                className="text-xs text-muted-foreground"
                data-testid="prompt-diff-already-current"
              >
                {t('chat.promptGallery.diff.alreadyCurrent')}
              </p>
            ) : null}

            {actionError || diffError ? (
              <p className="text-sm text-destructive" role="alert">
                {actionError ?? t('chat.promptGallery.diff.loadError')}
              </p>
            ) : null}

            <div className="flex-1 overflow-auto rounded-md border custom-scrollbar">
              {loadingDiff ? (
                <p className="p-3 text-sm text-muted-foreground">
                  {t('common.status.loading')}
                </p>
              ) : rows.length === 0 && diff ? (
                <p className="p-3 text-sm text-muted-foreground">
                  {t('chat.promptGallery.diff.noChanges')}
                </p>
              ) : (
                <VersionDiffRows rows={rows} viewMode={viewMode} />
              )}
            </div>

            {diff?.truncated ? (
              <p className="text-xs text-muted-foreground">
                {t('chat.promptGallery.diff.truncated')}
              </p>
            ) : null}

            {isPinned ? (
              <div className="flex items-center justify-end">
                <Button
                  type="button"
                  onClick={() => handleUpdate(false)}
                  disabled={
                    !targetId ||
                    syncing ||
                    alreadyCurrent ||
                    sourceUnavailable ||
                    confirmLocal
                  }
                  data-testid="prompt-diff-update"
                >
                  {targetVersion
                    ? t('chat.promptGallery.diff.update', {
                        version: targetVersion.version
                      })
                    : t('chat.promptGallery.diff.updateFallback')}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default PromptVersionDiff;
