import {
  MAX_SKILL_MARKDOWN_BYTES,
  SkillInput,
  downloadMarkdown,
  formatSkillTimestamp,
  skillErrorDetail
} from '@/lib/skills';
import { cn } from '@/lib/utils';
import {
  ISkill,
  promptErrorCode,
  useAuth,
  useConfig,
  useSkills
} from '@chainlit-ng/react-client';
import {
  ChevronDown,
  Download,
  Ellipsis,
  ListFilter,
  Pencil,
  Plus,
  RefreshCw,
  Share2,
  Trash2,
  Upload
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { SkillEditorDialog } from '@/components/SkillEditorDialog';
import { SkillReviewDialog } from '@/components/SkillReviewDialog';
import { SkillShareDialog } from '@/components/SkillShareDialog';
import { SkillUpdateDialog } from '@/components/SkillUpdateDialog';
import { useTranslation } from '@/components/i18n/Translator';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu';
import { Switch } from '@/components/ui/switch';

type EditTarget = ISkill | 'new' | undefined;
type Section = 'all' | 'active' | 'review' | 'updates';

interface UpdateTarget {
  skill: ISkill;
  versionId: string | null;
}

/** Imported and linked copies must be reviewed before reaching a model. */
function needsReview(skill: ISkill): boolean {
  if (!skill.importedFrom && !skill.sourcePromptId) return false;
  return !skill.reviewHash || skill.reviewedContentHash !== skill.reviewHash;
}

export function SkillsPanel() {
  const { t } = useTranslation();
  const { config } = useConfig();
  const { data: authConfig } = useAuth();
  const {
    skills,
    skillUpdates,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill,
    fetchSkillUpdates,
    detachSkill
  } = useSkills();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<EditTarget>();
  const [reviewing, setReviewing] = useState<ISkill | null>(null);
  const [reviewNonce, setReviewNonce] = useState(0);
  const [sharing, setSharing] = useState<ISkill | null>(null);
  const [updateTarget, setUpdateTarget] = useState<UpdateTarget | null>(null);
  const [section, setSection] = useState<Section>('all');
  const [importing, setImporting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const fallbackError = t('common.status.error.default');

  const sharingEnabled = config?.skillsSharing === true;
  const shareLinksEnabled = sharingEnabled && authConfig?.requireLogin === true;
  const libraryEnabled = config?.skillsLibrary !== false;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      fetchSkills(),
      sharingEnabled ? fetchSkillUpdates().catch(() => []) : Promise.resolve([])
    ])
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
  }, [fetchSkills, fetchSkillUpdates, sharingEnabled, fallbackError]);

  const handleToggle = async (skill: ISkill, enabled: boolean) => {
    setError(undefined);
    if (enabled && needsReview(skill)) {
      setReviewing(skill);
      return;
    }
    try {
      await updateSkill(skill.id, { enabled });
    } catch (reason) {
      if (promptErrorCode(reason) === 'content_changed') {
        toast.error(t('chat.skills.review.changed'));
        const refreshed = await fetchSkills().catch(() => null);
        const current = refreshed?.find((item) => item.id === skill.id);
        if (current) {
          setReviewing(current);
        } else {
          setError(t('chat.skills.review.changed'));
        }
      } else {
        setError(skillErrorDetail(reason) || t('common.status.error.default'));
      }
    }
  };

  const handleReviewEnable = async (reviewedContentHash: string) => {
    if (!reviewing) return;
    try {
      await updateSkill(reviewing.id, {
        enabled: true,
        reviewedContentHash
      });
      toast.success(t('chat.skills.review.enabled'));
      setReviewing(null);
    } catch (reason) {
      if (promptErrorCode(reason) === 'content_changed') {
        toast.error(t('chat.skills.review.changed'));
        setReviewNonce((value) => value + 1);
        await fetchSkills().catch(() => null);
        throw reason;
      }
      throw reason;
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

  const handleDetach = async (skill: ISkill) => {
    setError(undefined);
    try {
      await detachSkill(skill.id);
      toast.success(t('chat.skills.detached'));
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

  const updatesById = new Map(
    skillUpdates.map((update) => [update.skillId, update])
  );
  const reviewCount = skills.filter(
    (skill) => needsReview(skill) && !skill.enabled
  ).length;
  const activeCount = skills.filter((skill) => skill.enabled).length;
  const visibleSkills = skills.filter((skill) => {
    if (section === 'active') return !!skill.enabled;
    if (section === 'review') return needsReview(skill) && !skill.enabled;
    if (section === 'updates') return updatesById.has(skill.id);
    return true;
  });
  const sections: { id: Section; label: string; count: number }[] = [
    { id: 'all', label: t('chat.skills.sections.all'), count: skills.length },
    {
      id: 'active',
      label: t('chat.skills.sections.active'),
      count: activeCount
    },
    {
      id: 'review',
      label: t('chat.skills.sections.review'),
      count: reviewCount
    },
    {
      id: 'updates',
      label: t('chat.skills.sections.updates'),
      count: skillUpdates.length
    }
  ];

  const currentSection =
    sections.find((item) => item.id === section) ?? sections[0];

  return (
    <>
      <div className="flex flex-1 min-h-0 flex-col gap-3">
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
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="icon"
                className={cn(
                  'ml-auto h-8 w-8',
                  section !== 'all' && 'border-primary text-primary'
                )}
                aria-label={t('chat.skills.filter.current', {
                  filter: currentSection.label
                })}
                title={t('chat.skills.filter.current', {
                  filter: currentSection.label
                })}
                data-testid="skill-filter"
              >
                <ListFilter className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              <DropdownMenuRadioGroup
                value={section}
                onValueChange={(value) => setSection(value as Section)}
              >
                {sections.map((item) => (
                  <DropdownMenuRadioItem
                    key={item.id}
                    value={item.id}
                    data-testid={`skill-section-${item.id}`}
                  >
                    {item.label}
                    <span className="ml-auto text-xs text-muted-foreground">
                      {item.count}
                    </span>
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {!libraryEnabled ? (
          <p className="text-xs text-muted-foreground">
            {t('chat.skills.libraryUnavailable')}
          </p>
        ) : null}

        {error && (
          <p
            id="skill-error"
            role="alert"
            className="text-xs text-destructive break-words"
          >
            {error}
          </p>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto custom-scrollbar px-1">
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
          ) : visibleSkills.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t('chat.skills.sections.empty')}
            </p>
          ) : (
            <div className="divide-y">
              {visibleSkills.map((skill) => {
                const update = updatesById.get(skill.id);
                const gated = needsReview(skill);
                return (
                  <div
                    key={skill.id}
                    data-testid="skill-row"
                    data-skill-id={skill.id}
                    className="flex items-start gap-3 py-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="break-all text-sm font-medium">
                          {skill.title}
                        </span>
                        <button
                          type="button"
                          data-testid={`skill-versions-${skill.id}`}
                          className="inline-flex shrink-0 items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground hover:border-primary/50 hover:text-primary"
                          aria-label={t('chat.skills.versions', {
                            version: skill.pinnedVersion ?? skill.version ?? 1
                          })}
                          onClick={() =>
                            setUpdateTarget({ skill, versionId: null })
                          }
                        >
                          v{skill.pinnedVersion ?? skill.version ?? 1}
                          <ChevronDown className="h-2.5 w-2.5" />
                        </button>
                        {skill.importedFrom && (
                          <Badge
                            variant="outline"
                            title={t('chat.skills.importedBadgeTitle')}
                          >
                            {t('chat.skills.importedBadge')}
                          </Badge>
                        )}
                        {skill.sourcePromptId && (
                          <Badge variant="outline">
                            {t('chat.skills.fromLinkBadge')}
                          </Badge>
                        )}
                        {skill.isShared && (
                          <Badge>{t('chat.skills.sharedBadge')}</Badge>
                        )}
                        {update ? (
                          <button
                            type="button"
                            data-testid={`skill-update-chip-${skill.id}`}
                            className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary hover:bg-primary/20"
                            onClick={() =>
                              setUpdateTarget({
                                skill,
                                versionId: update.latestVersionId
                              })
                            }
                          >
                            {t('chat.skills.updateAvailable', {
                              version: update.latestVersion
                            })}
                          </button>
                        ) : null}
                      </div>
                      <p className="line-clamp-2 text-xs text-muted-foreground whitespace-pre-wrap">
                        {skill.description}
                      </p>
                      {skill.importedFrom && (
                        <p className="break-words text-xs text-muted-foreground">
                          {t('chat.skills.importedBy', {
                            source: skill.importedFrom,
                            date: formatSkillTimestamp(skill.importedAt)
                          })}
                        </p>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <Switch
                        checked={!!skill.enabled}
                        aria-label={
                          gated && !skill.enabled
                            ? t('chat.skills.enable', {
                                name: skill.title
                              })
                            : t('chat.skills.toggle', {
                                name: skill.title
                              })
                        }
                        onCheckedChange={(enabled) =>
                          handleToggle(skill, enabled)
                        }
                      />
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-muted-foreground hover:text-foreground"
                            aria-label={t('chat.skills.menu', {
                              name: skill.title
                            })}
                            data-testid={`skill-actions-${skill.id}`}
                          >
                            <Ellipsis className="h-3.5 w-3.5" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-48">
                          <DropdownMenuItem onClick={() => setEditing(skill)}>
                            {t('chat.skills.edit')}
                            <Pencil className="ml-auto" />
                          </DropdownMenuItem>
                          {shareLinksEnabled && (
                            <DropdownMenuItem
                              data-testid={`skill-share-${skill.id}`}
                              onClick={() => setSharing(skill)}
                            >
                              {t('chat.skills.share.title')}
                              <Share2 className="ml-auto" />
                            </DropdownMenuItem>
                          )}
                          <DropdownMenuItem onClick={() => handleExport(skill)}>
                            {t('chat.skills.export')}
                            <Download className="ml-auto" />
                          </DropdownMenuItem>
                          {sharingEnabled && skill.sourcePromptId && (
                            <DropdownMenuItem
                              data-testid={`skill-detach-${skill.id}`}
                              onClick={() => handleDetach(skill)}
                            >
                              {t('chat.skills.update.detach')}
                              <RefreshCw className="ml-auto" />
                            </DropdownMenuItem>
                          )}
                          <DropdownMenuItem
                            className="text-red-600 focus:bg-red-600/10 focus:text-red-600 dark:text-red-400 dark:focus:text-red-400"
                            onClick={() => handleDelete(skill)}
                          >
                            {t('chat.skills.delete')}
                            <Trash2 className="ml-auto" />
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </div>
                );
              })}
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

      <SkillReviewDialog
        key={`${reviewing?.id ?? 'none'}-${reviewNonce}`}
        open={!!reviewing}
        skill={reviewing}
        onEnable={handleReviewEnable}
        onClose={() => setReviewing(null)}
      />

      <SkillShareDialog
        open={!!sharing}
        skill={sharing}
        onClose={() => setSharing(null)}
        onChanged={(updated) => {
          setSharing(updated);
        }}
      />

      <SkillUpdateDialog
        open={!!updateTarget}
        skill={updateTarget?.skill ?? null}
        initialVersionId={updateTarget?.versionId ?? null}
        onOpenChange={(next) => {
          if (!next) setUpdateTarget(null);
        }}
        onUpdated={() => {
          setUpdateTarget(null);
        }}
      />
    </>
  );
}

export default SkillsPanel;
