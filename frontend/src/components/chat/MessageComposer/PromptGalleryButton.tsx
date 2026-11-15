import { copyToClipboard } from '@/lib/clipboard';
import getRouterBasename from '@/lib/router';
import { cn } from '@/lib/utils';
import {
  IPrompt,
  useAuth,
  useChainlitStore,
  useConfig,
  usePromptGallery,
  useSkills
} from '@chainlit-ng/react-client';
import {
  BookMarked,
  ChevronDown,
  Globe,
  GlobeLock,
  Pencil,
  Plus,
  Trash2
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { PromptSaveDialog } from '@/components/PromptSaveDialog';
import { PromptVersionDiff } from '@/components/PromptVersionDiff';
import Translator, { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandListScrollable
} from '@/components/ui/command';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';

import { SkillsPanel } from './SkillsPanel';

interface Props {
  disabled?: boolean;
  onSelect: (content: string) => void;
}

const LIBRARY_OPEN_EVENT = 'chainlit:library-open';

function requestLibrary(tab: 'prompts' | 'skills') {
  window.dispatchEvent(
    new CustomEvent(LIBRARY_OPEN_EVENT, { detail: { tab } })
  );
}

export function PromptGalleryButton({ disabled = false, onSelect }: Props) {
  const { config } = useConfig();
  const { data: authConfig } = useAuth();
  const { t } = useTranslation();
  const prompts = useChainlitStore((state) => state.prompts);
  const promptUpdates = useChainlitStore((state) => state.promptUpdates);
  const {
    fetchPrompts,
    editPrompt,
    removePrompt,
    sharePrompt,
    savePrompt,
    fetchPromptUpdates,
    ackPromptUpdate
  } = usePromptGallery();
  const { skillUpdates, fetchSkillUpdates, ackSkillUpdate } = useSkills();

  const [open, setOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<IPrompt | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [diffTarget, setDiffTarget] = useState<{
    prompt: IPrompt;
    versionId: string | null;
  } | null>(null);
  const promptsEnabled = config?.promptGallery === true;
  const skillsEnabled =
    config?.features?.skills === true && config?.skillsLibrary !== false;
  const versioningEnabled = promptsEnabled && config?.promptVersioning === true;
  const skillSharingEnabled = skillsEnabled && config?.skillsSharing === true;
  const shareLinksEnabled = authConfig?.requireLogin === true;
  const [tab, setTab] = useState<'prompts' | 'skills'>(
    promptsEnabled ? 'prompts' : 'skills'
  );

  // Toast actions can outlive a render; route them back to the mounted dialog
  // instead of closing over a possibly stale state setter.
  useEffect(() => {
    const handleOpen = (event: Event) => {
      const detail = (event as CustomEvent<{ tab?: 'prompts' | 'skills' }>)
        .detail;
      setTab(
        detail?.tab === 'skills'
          ? 'skills'
          : promptsEnabled
            ? 'prompts'
            : 'skills'
      );
      setOpen(true);
    };
    window.addEventListener(LIBRARY_OPEN_EVENT, handleOpen);
    return () => window.removeEventListener(LIBRARY_OPEN_EVENT, handleOpen);
  }, [promptsEnabled]);

  const hasFetchedRef = useRef(false);
  const fetchInFlightRef = useRef(false);
  useEffect(() => {
    if (
      open &&
      promptsEnabled &&
      !hasFetchedRef.current &&
      !fetchInFlightRef.current
    ) {
      fetchInFlightRef.current = true;
      Promise.resolve(fetchPrompts())
        .then(() => {
          hasFetchedRef.current = true;
        })
        .catch(() => null)
        .finally(() => {
          fetchInFlightRef.current = false;
        });
    }
  }, [open, promptsEnabled, fetchPrompts]);

  // Query updates once so the quiet dot is visible before the dialog opens.
  const hasFetchedUpdatesRef = useRef(false);
  useEffect(() => {
    if (!versioningEnabled || hasFetchedUpdatesRef.current) return;
    hasFetchedUpdatesRef.current = true;
    Promise.resolve(fetchPromptUpdates()).catch(() => {
      hasFetchedUpdatesRef.current = false;
    });
  }, [versioningEnabled, fetchPromptUpdates]);

  // Refresh on every open: the server clamps acknowledgements, the marker is
  // kept locally by the hook so it does not disappear after acking.
  useEffect(() => {
    if (!open || !versioningEnabled) return;
    Promise.resolve(fetchPromptUpdates()).catch(() => null);
  }, [open, versioningEnabled, fetchPromptUpdates]);

  const hasFetchedSkillUpdatesRef = useRef(false);
  useEffect(() => {
    if (!skillSharingEnabled || hasFetchedSkillUpdatesRef.current) return;
    hasFetchedSkillUpdatesRef.current = true;
    Promise.resolve(fetchSkillUpdates()).catch(() => {
      hasFetchedSkillUpdatesRef.current = false;
    });
  }, [skillSharingEnabled, fetchSkillUpdates]);

  useEffect(() => {
    if (!open || !skillSharingEnabled) return;
    Promise.resolve(fetchSkillUpdates()).catch(() => null);
  }, [open, skillSharingEnabled, fetchSkillUpdates]);

  // One grouped, dismissible toast per newly seen source version. The quiet
  // per-row marker stays; only the toast is dismissed by acknowledging.
  const toastedVersionsRef = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!versioningEnabled) return;
    const pending = promptUpdates.filter((update) => {
      const seen = Math.max(
        update.notifiedVersion,
        toastedVersionsRef.current[update.promptId] ?? 0
      );
      return update.latestVersion > seen;
    });
    if (pending.length === 0) return;
    for (const update of pending) {
      toastedVersionsRef.current[update.promptId] = update.latestVersion;
    }
    const count = pending.length;
    toast(t('chat.promptGallery.updatesToast', { count }), {
      id: 'prompt-gallery-updates',
      description:
        count === 1
          ? `${pending[0].title} → v${pending[0].latestVersion}`
          : undefined,
      action: {
        label: t('chat.promptGallery.updatesToastAction'),
        onClick: () => {
          // Let the click finish before the modal's outside-click guard runs.
          window.setTimeout(() => requestLibrary('prompts'), 0);
        }
      },
      duration: 8000
    });
    for (const update of pending) {
      ackPromptUpdate(update.promptId, update.latestVersion).catch(() => null);
    }
  }, [promptUpdates, versioningEnabled, ackPromptUpdate, t]);

  // Skills reuse the one-notice-per-source-version contract. The row chip and
  // the library dot stay after the toast is acknowledged.
  const toastedSkillVersionsRef = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!skillSharingEnabled) return;
    const pending = skillUpdates.filter(
      (update) =>
        update.latestVersion >
        Math.max(
          update.seenVersion ?? 0,
          toastedSkillVersionsRef.current[update.skillId] ?? 0
        )
    );
    if (pending.length === 0) return;
    for (const update of pending) {
      toastedSkillVersionsRef.current[update.skillId] = update.latestVersion;
    }
    const count = pending.length;
    toast(t('chat.skills.updatesToast', { count }), {
      id: 'skill-updates',
      description:
        count === 1
          ? `${pending[0].title} → v${pending[0].latestVersion}`
          : undefined,
      action: {
        label: t('chat.skills.updatesToastAction'),
        onClick: () => {
          // Let the click finish before the modal's outside-click guard runs.
          window.setTimeout(() => requestLibrary('skills'), 0);
        }
      },
      duration: 8000
    });
    for (const update of pending) {
      ackSkillUpdate(update.skillId, update.latestVersion).catch(() => null);
    }
  }, [skillUpdates, skillSharingEnabled, ackSkillUpdate, t]);

  const updatesByPrompt = new Map(
    promptUpdates.map((update) => [update.promptId, update])
  );
  const hasUpdates =
    promptUpdates.length > 0 ||
    (skillSharingEnabled && skillUpdates.length > 0);

  const handleShare = useCallback(
    async (prompt: IPrompt) => {
      const isCurrentlyShared = prompt.isShared;
      const newSharedState = !isCurrentlyShared;
      const shareUrl = await sharePrompt(prompt.id, newSharedState);
      if (newSharedState) {
        const basename = getRouterBasename().replace(/\/$/, '');
        const fullUrl = `${window.location.origin}${basename}${shareUrl}`;
        await copyToClipboard(fullUrl);
        toast.success(t('chat.promptGallery.copied'));
      } else {
        toast.success(t('chat.promptGallery.unshared'));
      }
    },
    [sharePrompt, t]
  );

  if (!promptsEnabled && !skillsEnabled) return null;

  return (
    <>
      <Dialog open={open} onOpenChange={setOpen}>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                id="prompt-gallery-open"
                variant="ghost"
                size="icon"
                aria-label={t('chat.promptGallery.button')}
                className={cn(
                  'relative rounded-full hover:bg-muted hover:dark:bg-muted transition-all duration-200',
                  open && 'bg-muted/50'
                )}
                disabled={disabled}
                onClick={() => setOpen(true)}
              >
                <BookMarked className="!size-6" />
                {hasUpdates ? (
                  <span
                    data-testid="prompt-gallery-update-dot"
                    aria-hidden="true"
                    className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-primary"
                  />
                ) : null}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              <p>
                {promptsEnabled
                  ? t('chat.promptGallery.button')
                  : t('chat.skills.button')}
              </p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <DialogContent
          className="sm:max-w-2xl flex flex-col max-h-[80vh] gap-4"
          aria-describedby={undefined}
        >
          <DialogHeader className="flex flex-col items-start justify-between gap-2 space-y-0 pr-8 sm:flex-row sm:items-center">
            <DialogTitle>{t('chat.promptGallery.title')}</DialogTitle>
            {tab === 'prompts' && promptsEnabled && (
              <Button
                id="prompt-gallery-new"
                size="sm"
                onClick={() => {
                  setCreateOpen(true);
                }}
              >
                <Plus className="h-4 w-4 mr-1" />
                {t('chat.promptGallery.new')}
              </Button>
            )}
          </DialogHeader>

          <Tabs
            value={tab}
            onValueChange={(value) => setTab(value as 'prompts' | 'skills')}
            className="flex-1 min-h-0 overflow-hidden flex flex-col"
          >
            {promptsEnabled && skillsEnabled && (
              <TabsList className="self-start">
                <TabsTrigger value="prompts">
                  {t('chat.promptGallery.tab')}
                </TabsTrigger>
                <TabsTrigger value="skills">{t('chat.skills.tab')}</TabsTrigger>
              </TabsList>
            )}

            {promptsEnabled && (
              <TabsContent
                value="prompts"
                className="flex-1 min-h-0 overflow-hidden flex flex-col"
              >
                <Command className="flex-1 overflow-hidden border rounded-md">
                  <CommandInput placeholder={t('chat.promptGallery.search')} />
                  <CommandListScrollable className="max-h-[50vh] custom-scrollbar">
                    {prompts.length === 0 ? (
                      <CommandEmpty className="py-8 px-4">
                        <div className="flex flex-col items-center gap-2 text-center">
                          <p className="text-sm font-medium text-foreground">
                            <Translator path="chat.promptGallery.empty.title" />
                          </p>
                          <p className="text-xs text-muted-foreground">
                            <Translator path="chat.promptGallery.empty.description" />
                          </p>
                        </div>
                      </CommandEmpty>
                    ) : (
                      <CommandGroup>
                        {prompts.map((prompt) => (
                          <CommandItem
                            key={prompt.id}
                            value={`${prompt.id} ${prompt.title} ${prompt.content}`}
                            onSelect={() => {
                              onSelect(prompt.content);
                              setOpen(false);
                            }}
                            className="cursor-pointer flex-col items-start py-4"
                          >
                            <div className="flex items-start justify-between gap-2 w-full overflow-hidden">
                              <div className="flex flex-col gap-1 overflow-hidden">
                                <div className="flex min-w-0 items-center gap-2">
                                  <span className="truncate text-sm font-medium">
                                    {prompt.title}
                                  </span>
                                  {versioningEnabled ? (
                                    <button
                                      type="button"
                                      data-testid={`prompt-versions-${prompt.id}`}
                                      className="inline-flex shrink-0 items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground hover:border-primary/50 hover:text-primary"
                                      aria-label={t(
                                        'chat.promptGallery.versions',
                                        {
                                          version:
                                            prompt.pinnedVersion ??
                                            prompt.version ??
                                            1
                                        }
                                      )}
                                      onPointerDown={(e) => e.stopPropagation()}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setDiffTarget({
                                          prompt,
                                          versionId: null
                                        });
                                        setOpen(false);
                                      }}
                                    >
                                      v
                                      {prompt.pinnedVersion ??
                                        prompt.version ??
                                        1}
                                      <ChevronDown className="h-2.5 w-2.5" />
                                    </button>
                                  ) : null}
                                </div>
                                {updatesByPrompt.get(prompt.id) ? (
                                  <button
                                    type="button"
                                    data-testid={`prompt-update-chip-${prompt.id}`}
                                    className="self-start rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary hover:bg-primary/20"
                                    onPointerDown={(e) => e.stopPropagation()}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      const update = updatesByPrompt.get(
                                        prompt.id
                                      );
                                      setDiffTarget({
                                        prompt,
                                        versionId:
                                          update?.latestVersionId ?? null
                                      });
                                      setOpen(false);
                                    }}
                                  >
                                    {t('chat.promptGallery.updateAvailable', {
                                      version: updatesByPrompt.get(prompt.id)
                                        ?.latestVersion
                                    })}
                                  </button>
                                ) : null}
                                <span className="line-clamp-2 text-xs text-muted-foreground whitespace-pre-wrap">
                                  {prompt.content}
                                </span>
                              </div>
                              <div className="flex items-center gap-1 shrink-0">
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-muted-foreground hover:text-foreground"
                                  aria-label={t('chat.promptGallery.edit')}
                                  onPointerDown={(e) => e.stopPropagation()}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setEditTarget(prompt);
                                    setOpen(false);
                                  }}
                                >
                                  <Pencil className="h-3.5 w-3.5" />
                                </Button>
                                {shareLinksEnabled ? (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="icon"
                                    className={cn(
                                      'h-7 w-7',
                                      prompt.isShared
                                        ? 'text-blue-500 hover:text-blue-600'
                                        : 'text-muted-foreground hover:text-foreground'
                                    )}
                                    aria-label={
                                      prompt.isShared
                                        ? t('chat.promptGallery.unshare')
                                        : t('chat.promptGallery.share.title')
                                    }
                                    onPointerDown={(e) => e.stopPropagation()}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      handleShare(prompt).catch(() =>
                                        toast.error(
                                          t('common.status.error.default')
                                        )
                                      );
                                    }}
                                  >
                                    {prompt.isShared ? (
                                      <GlobeLock className="h-3.5 w-3.5" />
                                    ) : (
                                      <Globe className="h-3.5 w-3.5" />
                                    )}
                                  </Button>
                                ) : null}
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-muted-foreground hover:text-foreground"
                                  aria-label={t('chat.promptGallery.delete')}
                                  onPointerDown={(e) => e.stopPropagation()}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    removePrompt(prompt.id).catch(() =>
                                      toast.error(
                                        t('common.status.error.default')
                                      )
                                    );
                                  }}
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </div>
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                  </CommandListScrollable>
                </Command>
              </TabsContent>
            )}

            {skillsEnabled && (
              <TabsContent
                value="skills"
                className="flex-1 min-h-0 overflow-hidden flex flex-col"
              >
                <SkillsPanel />
              </TabsContent>
            )}
          </Tabs>
        </DialogContent>
      </Dialog>

      {editTarget && (
        <PromptSaveDialog
          open
          editing
          initialTitle={editTarget.title}
          initialContent={editTarget.content}
          onSave={async (title, content) => {
            await editPrompt(editTarget.id, { title, content });
            setEditTarget(null);
            setOpen(true);
          }}
          onClose={() => {
            setEditTarget(null);
            setOpen(true); // reopen gallery when edit is cancelled/closed
          }}
          onError={() => toast.error(t('common.status.error.default'))}
        />
      )}

      <PromptSaveDialog
        open={createOpen}
        onSave={async (title, content) => {
          await savePrompt(title, content);
          setCreateOpen(false);
          toast.success(t('chat.promptGallery.saved'));
        }}
        onClose={() => setCreateOpen(false)}
        onError={() => toast.error(t('common.status.error.default'))}
      />

      <PromptVersionDiff
        prompt={diffTarget?.prompt ?? null}
        open={diffTarget !== null}
        initialVersionId={diffTarget?.versionId ?? null}
        onOpenChange={(next) => {
          if (!next) {
            setDiffTarget(null);
            setOpen(true);
          }
        }}
      />
    </>
  );
}

export default PromptGalleryButton;
