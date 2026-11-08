import {
  SkillInput,
  SkillValidationErrors,
  formatSkillTimestamp,
  skillErrorDetail,
  validateSkill
} from '@/lib/skills';
import { useEffect, useState } from 'react';

import { ISkill, useConfig } from '@chainlit/react-client';

import { Markdown } from '@/components/Markdown';
import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';

interface Props {
  open: boolean;
  skill?: ISkill;
  onSave: (input: SkillInput) => Promise<void>;
  onClose: () => void;
}

export function SkillEditorDialog({ open, skill, onSave, onClose }: Props) {
  const { t } = useTranslation();
  const { config } = useConfig();
  const [name, setName] = useState(skill?.title || '');
  const [description, setDescription] = useState(skill?.description || '');
  const [content, setContent] = useState(skill?.content || '');
  const [errors, setErrors] = useState<SkillValidationErrors>({});
  const [serverError, setServerError] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(skill?.title || '');
    setDescription(skill?.description || '');
    setContent(skill?.content || '');
    setErrors({});
    setServerError(undefined);
  }, [open, skill]);

  const handleSave = async () => {
    const input: SkillInput = {
      name: name.trim(),
      description: description.trim(),
      content
    };
    const nextErrors = validateSkill(input);
    setErrors(nextErrors);
    setServerError(undefined);
    if (Object.keys(nextErrors).length > 0) return;

    setSaving(true);
    try {
      await onSave(input);
      onClose();
    } catch (error) {
      setServerError(
        skillErrorDetail(error) || t('common.status.error.default')
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent
        className="sm:max-w-3xl flex flex-col max-h-[85vh]"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle>
            {skill
              ? t('chat.skills.editor.editTitle')
              : t('chat.skills.editor.createTitle')}
          </DialogTitle>
        </DialogHeader>

        {skill?.importedFrom && (
          <p className="text-xs text-muted-foreground break-all">
            {t('chat.skills.importedBy', {
              source: skill.importedFrom,
              date: formatSkillTimestamp(skill.importedAt)
            })}
            {skill.contentHash
              ? ` · sha256:${skill.contentHash.slice(0, 12)}…`
              : ''}
          </p>
        )}

        <div className="flex flex-col gap-3 overflow-y-auto pr-1">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="skill-name">{t('chat.skills.editor.name')}</Label>
            <Input
              id="skill-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="release-notes"
              aria-invalid={!!errors.name}
              aria-describedby={errors.name ? 'skill-name-error' : undefined}
              autoFocus
            />
            <p className="text-xs text-muted-foreground">
              {t('chat.skills.editor.nameHint')}
            </p>
            {errors.name && (
              <p id="skill-name-error" className="text-xs text-destructive">
                {t(errors.name.key, errors.name.values)}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="skill-description">
              {t('chat.skills.editor.description')}
            </Label>
            <Input
              id="skill-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              aria-invalid={!!errors.description}
              aria-describedby={
                errors.description ? 'skill-description-error' : undefined
              }
            />
            <p className="text-xs text-muted-foreground">
              {t('chat.skills.editor.descriptionHint')}
            </p>
            {errors.description && (
              <p
                id="skill-description-error"
                className="text-xs text-destructive"
              >
                {t(errors.description.key, errors.description.values)}
              </p>
            )}
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="skill-content">
                {t('chat.skills.editor.body')}
              </Label>
              <Textarea
                id="skill-content"
                value={content}
                onChange={(event) => setContent(event.target.value)}
                rows={12}
                className="resize-none font-mono text-xs"
                aria-invalid={!!errors.content}
                aria-describedby={
                  errors.content ? 'skill-content-error' : undefined
                }
              />
              {errors.content && (
                <p
                  id="skill-content-error"
                  className="text-xs text-destructive"
                >
                  {t(errors.content.key, errors.content.values)}
                </p>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>{t('chat.skills.editor.preview')}</Label>
              <div className="h-[242px] overflow-y-auto rounded-md border p-3">
                {content ? (
                  <Markdown
                    allowHtml={false}
                    latex={config?.features?.latex}
                    className="prose-sm max-w-none"
                  >
                    {content}
                  </Markdown>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {t('chat.skills.editor.previewEmpty')}
                  </p>
                )}
              </div>
            </div>
          </div>

          {serverError && (
            <p role="alert" className="text-xs text-destructive break-words">
              {serverError}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            {t('chat.skills.editor.cancel')}
          </Button>
          <Button id="skill-save" onClick={handleSave} disabled={saving}>
            {saving
              ? t('chat.skills.editor.saving')
              : t('chat.skills.editor.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
