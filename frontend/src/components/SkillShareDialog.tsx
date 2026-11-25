import { absoluteShareUrl, copyToClipboard } from '@/lib/clipboard';
import getRouterBasename from '@/lib/router';
import { skillErrorDetail } from '@/lib/skills';
import { ISkill, useSkills } from '@chainlit-ng/react-client';
import { Copy, Globe, GlobeLock } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { useTranslation } from '@/components/i18n/Translator';
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
  onClose: () => void;
  onChanged?: (skill: ISkill) => void;
}
const AUTHOR_LABEL_DISCLOSURE =
  'Anyone with the link can see the author label (your name or email).';

/**
 * Small share surface for one skill: a link can be copied only while the skill
 * is shared, and unsharing hides every public route immediately.
 */
export function SkillShareDialog({ open, skill, onClose, onChanged }: Props) {
  const { t } = useTranslation();
  const { shareSkill } = useSkills();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [shared, setShared] = useState(!!skill?.isShared);

  useEffect(() => {
    if (open) {
      setShared(!!skill?.isShared);
      setError(undefined);
    }
  }, [open, skill]);

  const link = skill
    ? absoluteShareUrl(
        `${getRouterBasename().replace(/\/$/, '')}/skill/${skill.id}`
      )
    : '';

  const handleCopy = async () => {
    try {
      await copyToClipboard(link);
      toast.success(t('chat.skills.share.copied'));
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('chat.skills.share.error'));
    }
  };

  const handleShare = async () => {
    if (!skill) return;
    setBusy(true);
    setError(undefined);
    try {
      const result = await shareSkill(skill.id, true);
      setShared(true);
      onChanged?.(result.skill);
      await copyToClipboard(link);
      toast.success(t('chat.skills.share.copied'));
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('chat.skills.share.error'));
    } finally {
      setBusy(false);
    }
  };

  const handleUnshare = async () => {
    if (!skill) return;
    setBusy(true);
    setError(undefined);
    try {
      const result = await shareSkill(skill.id, false);
      setShared(false);
      onChanged?.(result.skill);
      toast.success(t('chat.skills.share.unshared'));
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('chat.skills.share.error'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent
        className="sm:max-w-md flex flex-col gap-4"
        aria-describedby={undefined}
        data-testid="skill-share-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {shared ? (
              <Globe className="h-4 w-4" />
            ) : (
              <GlobeLock className="h-4 w-4" />
            )}
            {t('chat.skills.share.title')}
          </DialogTitle>
          <DialogDescription>
            {shared
              ? t('chat.skills.share.description')
              : t('chat.skills.share.descriptionOff')}
          </DialogDescription>
          <p className="text-xs text-muted-foreground">
            {t('chat.share.authorLabelDisclosure', {
              defaultValue: AUTHOR_LABEL_DISCLOSURE
            })}
          </p>
        </DialogHeader>

        {shared ? (
          <div className="flex items-center gap-2">
            <input
              readOnly
              value={link}
              aria-label={t('chat.skills.share.link')}
              data-testid="skill-share-link"
              className="h-9 min-w-0 flex-1 rounded-md border bg-muted px-2 text-xs"
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleCopy}
              disabled={busy}
            >
              <Copy className="mr-1 h-3.5 w-3.5" />
              {t('chat.skills.share.copy')}
            </Button>
          </div>
        ) : null}

        {error ? (
          <p role="alert" className="text-xs text-destructive break-words">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          {shared ? (
            <Button
              id="skill-unshare"
              variant="destructive"
              onClick={handleUnshare}
              disabled={busy}
            >
              {t('chat.skills.share.unshare')}
            </Button>
          ) : (
            <Button id="skill-share" onClick={handleShare} disabled={busy}>
              {t('chat.skills.share.submit')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default SkillShareDialog;
