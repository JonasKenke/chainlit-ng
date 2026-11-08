import {
  MAX_SKILL_MARKDOWN_BYTES,
  SkillInput,
  downloadMarkdown,
  formatSkillTimestamp,
  skillErrorDetail
} from '@/lib/skills';
import { Download, Pencil, Plus, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { ISkill, useSkills } from '@chainlit/react-client';

import { SkillEditorDialog } from '@/components/SkillEditorDialog';
import { useTranslation } from '@/components/i18n/Translator';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';

type EditTarget = ISkill | 'new' | undefined;

export function SkillsPanel() {
  const { t } = useTranslation();
  const {
    skills,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill
  } = useSkills();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<EditTarget>();
  const [importing, setImporting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const fallbackError = t('common.status.error.default');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSkills()
      .catch((reason) => {
        if (!cancelled) {
          setError(skillErrorDetail(reason) || fallbackError);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchSkills, fallbackError]);

  const handleToggle = async (skill: ISkill, enabled: boolean) => {
    setError(undefined);
    try {
      await updateSkill(skill.id, { enabled });
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('common.status.error.default'));
    }
  };

  const handleDelete = async (skill: ISkill) => {
    setError(undefined);
    try {
      await deleteSkill(skill.id);
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('common.status.error.default'));
    }
  };

  const handleExport = async (skill: ISkill) => {
    setError(undefined);
    try {
      const exported = await exportSkill(skill.id);
      downloadMarkdown(exported.markdown, exported.filename);
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('common.status.error.default'));
    }
  };

  const handleFile = async (file?: File) => {
    if (!file) return;
    setError(undefined);
    if (file.size > MAX_SKILL_MARKDOWN_BYTES) {
      setError(
        t('chat.skills.validation.fileTooLarge', {
          size: file.size,
          max: MAX_SKILL_MARKDOWN_BYTES
        })
      );
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }
    setImporting(true);
    try {
      await importSkill(await file.text(), file.name);
      toast.success(t('chat.skills.imported'));
    } catch (reason) {
      setError(skillErrorDetail(reason) || t('common.status.error.default'));
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleSave = async (input: SkillInput) => {
    if (editing === 'new') {
      await createSkill({ ...input, enabled: true });
      toast.success(t('chat.skills.created'));
    } else if (editing) {
      await updateSkill(editing.id, input);
      toast.success(t('chat.skills.saved'));
    }
  };

  return (
    <>
      <div className="flex flex-col gap-3 min-h-0">
        <div className="flex items-center gap-2">
          <Button id="skill-new" size="sm" onClick={() => setEditing('new')}>
            <Plus className="h-4 w-4 mr-1" />
            {t('chat.skills.new')}
          </Button>
          <Button
            id="skill-import"
            size="sm"
            variant="outline"
            disabled={importing}
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="h-4 w-4 mr-1" />
            {importing ? t('chat.skills.importing') : t('chat.skills.import')}
          </Button>
          <input
            ref={fileInputRef}
            id="skill-import-input"
            type="file"
            accept=".md,text/markdown,text/plain"
            className="hidden"
            onChange={(event) => handleFile(event.target.files?.[0])}
          />
        </div>

        {error && (
          <p
            id="skill-error"
            role="alert"
            className="text-xs text-destructive break-words"
          >
            {error}
          </p>
        )}

        <div className="max-h-[50vh] overflow-y-auto custom-scrollbar">
          {loading ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t('chat.skills.loading')}
            </p>
          ) : skills.length === 0 ? (
            <div className="py-8 px-4 text-center">
              <p className="text-sm font-medium text-foreground">
                {t('chat.skills.empty.title')}
              </p>
              <p className="text-xs text-muted-foreground">
                {t('chat.skills.empty.description')}
              </p>
            </div>
          ) : (
            <div className="divide-y">
              {skills.map((skill) => (
                <div
                  key={skill.id}
                  data-testid="skill-row"
                  className="flex items-start gap-3 py-3"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="break-all text-sm font-medium">
                        {skill.title}
                      </span>
                      {skill.importedFrom && (
                        <Badge
                          variant="outline"
                          title={t('chat.skills.importedBadgeTitle')}
                        >
                          {t('chat.skills.importedBadge')}
                        </Badge>
                      )}
                    </div>
                    <p className="line-clamp-2 text-xs text-muted-foreground whitespace-pre-wrap">
                      {skill.description}
                    </p>
                    {skill.importedFrom && (
                      <p
                        className="break-all text-xs text-muted-foreground"
                        title={
                          skill.contentHash
                            ? `sha256:${skill.contentHash}`
                            : undefined
                        }
                      >
                        {t('chat.skills.provenance', {
                          source: skill.importedFrom,
                          date: formatSkillTimestamp(skill.importedAt),
                          hash: skill.contentHash
                            ? `${skill.contentHash.slice(0, 12)}…`
                            : '—'
                        })}
                      </p>
                    )}
                    {skill.importedFrom && !skill.enabled && (
                      <p className="text-xs text-amber-600">
                        {t('chat.skills.reviewBeforeEnabling')}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Switch
                      checked={!!skill.enabled}
                      aria-label={t('chat.skills.toggle', {
                        name: skill.title
                      })}
                      onCheckedChange={(enabled) =>
                        handleToggle(skill, enabled)
                      }
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      aria-label={t('chat.skills.edit')}
                      onClick={() => setEditing(skill)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      aria-label={t('chat.skills.export')}
                      onClick={() => handleExport(skill)}
                    >
                      <Download className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      aria-label={t('chat.skills.delete')}
                      onClick={() => handleDelete(skill)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <SkillEditorDialog
        open={!!editing}
        skill={editing === 'new' ? undefined : editing}
        onSave={handleSave}
        onClose={() => setEditing(undefined)}
      />
    </>
  );
}
