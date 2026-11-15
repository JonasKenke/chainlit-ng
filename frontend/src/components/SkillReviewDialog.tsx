import { skillErrorDetail } from '@/lib/skills';
import {
  ISkill,
  ISkillVersionSummary,
  useConfig,
  useSkills
} from '@chainlit-ng/react-client';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Markdown } from '@/components/Markdown';
import { useTranslation } from '@/components/i18n/Translator';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';

interface Props {
  open: boolean;
  skill: ISkill | null;
  onEnable: (reviewedContentHash: string) => Promise<void>;
  onClose: () => void;
}

function shortHash(hash?: string | null): string {
  if (!hash) return '—';
  return hash.length > 18 ? `${hash.slice(0, 18)}…` : hash;
}

/**
 * Review gate for imported and pinned skills. The body is rendered as sanitized
 * Markdown (`allowHtml={false}`) and the fingerprint shown here is the one the
 * server compares before enabling.
 */
export function SkillReviewDialog({ open, skill, onEnable, onClose }: Props) {
  const { t } = useTranslation();
  const { config } = useConfig();
  const { fetchSkillVersions } = useSkills();
  const [head, setHead] = useState<ISkillVersionSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!open || !skill) return;
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    fetchSkillVersions(skill.id)
      .then((versions) => {
        if (cancelled) return;
        const newest = [...versions].sort((a, b) => b.version - a.version)[0];
        setHead(newest ?? null);
      })
      .catch(() => {
        if (cancelled) return;
        // A data layer without the versions route still gates imports and
        // accepts the row's fingerprint; keep the gate usable instead of
        // dead-ending the dialog.
        setHead(
          skill.contentHash
            ? {
                id: skill.currentVersionId ?? skill.id,
                promptId: skill.id,
                kind: 'skill',
                version: skill.version ?? 1,
                title: skill.title,
                description: skill.description ?? '',
                contentHash: skill.contentHash,
                createdAt: skill.updatedAt
              }
            : null
        );
        if (!skill.contentHash) {
          setError(t('common.status.error.default'));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, skill, fetchSkillVersions, t]);

  const handleEnable = async () => {
    if (!head) return;
    setEnabling(true);
    setError(undefined);
    try {
      await onEnable(head.contentHash);
      onClose();
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('common.status.error.default'));
    } finally {
      setEnabling(false);
    }
  };

  const provenance = skill?.importedFrom
    ? t('chat.skills.importedBy', {
        source: skill.importedFrom,
        date: skill.importedAt
          ? new Date(skill.importedAt).toLocaleString()
          : '—'
      })
    : skill?.sourcePromptId
      ? t('chat.skills.review.fromLink')
      : '';

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent
        className="sm:max-w-3xl flex flex-col max-h-[85vh]"
        aria-describedby={undefined}
        data-testid="skill-review-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4" />
            {t('chat.skills.review.title')}
          </DialogTitle>
          <DialogDescription>
            {t('chat.skills.review.description')}
          </DialogDescription>
        </DialogHeader>

        {skill ? (
          <div className="flex flex-col gap-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center gap-2">
              <span className="break-all text-sm font-medium text-foreground">
                {skill.title}
              </span>
              {skill.importedFrom ? (
                <Badge variant="outline">
                  {t('chat.skills.importedBadge')}
                </Badge>
              ) : null}
              {skill.sourcePromptId ? (
                <Badge variant="outline">
                  {t('chat.skills.fromLinkBadge')}
                </Badge>
              ) : null}
              {head ? <Badge>{`v${head.version}`}</Badge> : null}
            </div>
            {provenance ? <p className="break-all">{provenance}</p> : null}
            <p className="break-all font-mono" data-testid="skill-review-hash">
              {t('chat.skills.review.fingerprint', {
                hash: shortHash(head?.contentHash ?? skill.contentHash)
              })}
            </p>
          </div>
        ) : null}

        <div
          className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400"
          data-testid="skill-review-warning"
        >
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{t('chat.skills.review.warning')}</span>
        </div>

        <div className="flex-1 overflow-y-auto rounded-md border p-3 custom-scrollbar">
          {loading ? (
            <p className="text-sm text-muted-foreground">
              {t('common.status.loading')}
            </p>
          ) : (
            <Markdown
              allowHtml={false}
              latex={config?.features?.latex}
              className="prose-sm max-w-none"
            >
              {skill?.content ?? ''}
            </Markdown>
          )}
        </div>

        {error ? (
          <p role="alert" className="text-xs text-destructive break-words">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={enabling}>
            {t('common.actions.cancel')}
          </Button>
          <Button
            id="skill-review-enable"
            onClick={handleEnable}
            disabled={!head || loading || enabling}
          >
            {enabling
              ? t('chat.skills.review.enabling')
              : t('chat.skills.review.enable')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default SkillReviewDialog;
