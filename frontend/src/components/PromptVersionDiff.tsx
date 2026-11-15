import { cn } from '@/lib/utils';
import {
  IPrompt,
  IPromptDiff,
  IPromptVersionSummary,
  promptErrorCode,
  usePromptGallery
} from '@chainlit-ng/react-client';
import { AlertTriangle, GitCompare, ShieldAlert } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

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

type ViewMode = 'unified' | 'split';

function shortHash(hash?: string | null): string {
  if (!hash) return '—';
  return hash.length > 18 ? `${hash.slice(0, 18)}…` : hash;
}

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
  const { fetchPromptVersions, fetchPromptDiff, syncPrompt, detachPrompt } =
    usePromptGallery();

  const [versions, setVersions] = useState<IPromptVersionSummary[]>([]);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [diff, setDiff] = useState<IPromptDiff | null>(null);
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

  const loadDiff = useCallback(async () => {
    if (!promptId || !targetId) {
      setDiff(null);
      return;
    }
    setLoadingDiff(true);
    setDiffError(false);
    try {
      const result = await fetchPromptDiff(promptId, targetId, 'pinned');
      setDiff(result);
    } catch (error) {
      const code = promptErrorCode(error);
      if (code === 'pinned_unavailable' || code === 'not_pinned') {
        setSourceUnavailable(true);
        setDiff(null);
      } else {
        setDiffError(true);
      }
    } finally {
      setLoadingDiff(false);
    }
  }, [promptId, targetId, fetchPromptDiff]);

  const loadVersions = useCallback(async () => {
    if (!promptId) return;
    setLoadingVersions(true);
    setVersionsError(false);
    try {
      const list = await fetchPromptVersions(promptId);
      const sorted = [...list].sort((a, b) => b.version - a.version);
      setVersions(sorted);
      const preferred = sorted.some(
        (version) => version.id === initialVersionId
      )
        ? initialVersionId
        : sorted[0]?.id;
      setTargetId(preferred ?? null);
    } catch {
      setVersions([]);
      setVersionsError(true);
    } finally {
      setLoadingVersions(false);
    }
  }, [promptId, fetchPromptVersions, initialVersionId]);

  useEffect(() => {
    if (!open || !promptId) return;
    setConfirmLocal(false);
    setStale(false);
    setSourceUnavailable(false);
    setAlreadyCurrent(false);
    setActionError(null);
    void loadVersions();
  }, [open, promptId, initialVersionId, loadVersions]);

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
        className="sm:max-w-3xl flex flex-col max-h-[85vh] gap-4"
        data-testid="prompt-diff-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GitCompare className="h-4 w-4" />
            {t('chat.promptGallery.diff.title')}
          </DialogTitle>
          <DialogDescription>
            {t('chat.promptGallery.diff.description')}
          </DialogDescription>
        </DialogHeader>

        {loadingVersions ? (
          <p className="text-sm text-muted-foreground">
            {t('common.status.loading')}
          </p>
        ) : versionsError ? (
          <p className="text-sm text-destructive">
            {t('chat.promptGallery.diff.loadError')}
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1">
                <label
                  htmlFor="prompt-diff-version"
                  className="text-xs text-muted-foreground"
                >
                  {t('chat.promptGallery.diff.version')}
                </label>
                <select
                  id="prompt-diff-version"
                  data-testid="prompt-diff-version"
                  value={targetId ?? ''}
                  onChange={(event) => setTargetId(event.target.value)}
                  className="h-9 min-w-40 rounded-md border bg-background px-2 text-sm"
                >
                  {versions.map((version) => (
                    <option key={version.id} value={version.id}>
                      {`v${version.version}`}
                    </option>
                  ))}
                </select>
              </div>
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
              <dl
                className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4"
                data-testid="prompt-diff-metadata"
              >
                <div>
                  <dt>{t('chat.promptGallery.diff.metaVersion')}</dt>
                  <dd className="text-foreground">v{targetVersion.version}</dd>
                </div>
                <div>
                  <dt>{t('chat.promptGallery.diff.metaDate')}</dt>
                  <dd className="text-foreground">
                    <time dateTime={targetVersion.createdAt}>
                      {formatDate(targetVersion.createdAt)}
                    </time>
                  </dd>
                </div>
                <div>
                  <dt>{t('chat.promptGallery.diff.metaHash')}</dt>
                  <dd className="text-foreground font-mono">
                    {shortHash(targetVersion.contentHash)}
                  </dd>
                </div>
                <div>
                  <dt>{t('chat.promptGallery.diff.metaAuthor')}</dt>
                  <dd className="text-foreground">
                    {targetVersion.createdBy
                      ? t('chat.promptGallery.diff.authorLabel', {
                          author: targetVersion.createdBy
                        })
                      : '—'}
                  </dd>
                </div>
              </dl>
            ) : null}

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
              ) : viewMode === 'unified' ? (
                <table className="w-full border-collapse text-xs font-mono">
                  <tbody>
                    {rows.map((row, index) => (
                      <tr
                        key={index}
                        data-testid="prompt-diff-row"
                        data-op={row.op}
                        className={cn(
                          row.op === 'insert' && 'bg-emerald-500/10',
                          row.op === 'delete' && 'bg-destructive/10',
                          row.op === 'replace' && 'bg-amber-500/10'
                        )}
                      >
                        <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
                          {row.oldLine ?? ''}
                        </td>
                        <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
                          {row.newLine ?? ''}
                        </td>
                        <td className="whitespace-pre-wrap px-2 py-0.5">
                          {row.op === 'equal' ? (
                            <span>{row.oldText ?? ''}</span>
                          ) : row.op === 'replace' ? (
                            <span>
                              <span className="block text-destructive line-through decoration-destructive/60">
                                {row.oldText ?? ''}
                              </span>
                              <span className="block text-emerald-600 dark:text-emerald-400">
                                {row.newText ?? ''}
                              </span>
                            </span>
                          ) : (
                            <span
                              className={
                                row.op === 'insert'
                                  ? 'text-emerald-600 dark:text-emerald-400'
                                  : 'text-destructive'
                              }
                            >
                              {row.op === 'insert'
                                ? (row.newText ?? '')
                                : (row.oldText ?? '')}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table className="w-full border-collapse text-xs font-mono">
                  <tbody>
                    {rows.map((row, index) => (
                      <tr
                        key={index}
                        data-testid="prompt-diff-row"
                        data-op={row.op}
                        className={cn(
                          row.op === 'insert' && 'bg-emerald-500/10',
                          row.op === 'delete' && 'bg-destructive/10',
                          row.op === 'replace' && 'bg-amber-500/10'
                        )}
                      >
                        <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
                          {row.oldLine ?? ''}
                        </td>
                        <td
                          className="w-1/2 whitespace-pre-wrap border-r px-2 py-0.5"
                          data-side="old"
                        >
                          {row.oldText ?? ''}
                        </td>
                        <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
                          {row.newLine ?? ''}
                        </td>
                        <td
                          className="w-1/2 whitespace-pre-wrap px-2 py-0.5"
                          data-side="new"
                        >
                          {row.newText ?? ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {diff?.truncated ? (
              <p className="text-xs text-muted-foreground">
                {t('chat.promptGallery.diff.truncated')}
              </p>
            ) : null}

            <div className="flex items-center justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                {t('common.actions.close')}
              </Button>
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
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default PromptVersionDiff;
