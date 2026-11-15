import getRouterBasename from '@/lib/router';
import {
  ISharedSkill,
  ISkillVersionSummary,
  promptErrorCode,
  useAuth,
  useConfig,
  useSkills
} from '@chainlit-ng/react-client';
import { GlobeLock, ShieldAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import Page from 'pages/Page';

import { Markdown } from '@/components/Markdown';
import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

const LATEST = 'latest';

/**
 * Public preview of a shared skill. Adding always copies it disabled and
 * pinned; the review dialog in the library is the only place it can be
 * enabled for the model.
 */
export default function SkillSharePage() {
  const { id } = useParams<{ id: string }>();
  const { config } = useConfig();
  const { data: authConfig, isAuthenticated } = useAuth();
  const {
    getSharedSkill,
    addSharedSkill,
    fetchSkillVersions,
    fetchSkillVersion
  } = useSkills();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [skill, setSkill] = useState<ISharedSkill | null>(null);
  const [versions, setVersions] = useState<ISkillVersionSummary[]>([]);
  const [selected, setSelected] = useState<string>(LATEST);
  const [previewContent, setPreviewContent] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState(false);
  const [alreadyAdded, setAlreadyAdded] = useState(false);
  const [nameConflict, setNameConflict] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const sharingEnabled = config?.skillsSharing === true;
  const libraryEnabled =
    config?.features?.skills === true && config?.skillsLibrary !== false;

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setLoading(true);
    getSharedSkill(id)
      .then((value) => {
        if (cancelled) return;
        setSkill(value);
        setPreviewContent(value.content);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setNotFound(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, getSharedSkill]);

  useEffect(() => {
    if (!id || !sharingEnabled) return;
    let cancelled = false;
    fetchSkillVersions(id, { shared: true })
      .then((list) => {
        if (!cancelled)
          setVersions([...list].sort((a, b) => b.version - a.version));
      })
      .catch(() => {
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [id, sharingEnabled, fetchSkillVersions]);

  useEffect(() => {
    if (!id || selected === LATEST) {
      setPreviewContent(skill?.content ?? '');
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    fetchSkillVersion(id, selected, { shared: true })
      .then((version) => {
        if (!cancelled) setPreviewContent(version.content);
      })
      .catch(() => {
        if (!cancelled) setPreviewContent(skill?.content ?? '');
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, selected, fetchSkillVersion, skill?.content]);

  const selectedVersion =
    selected === LATEST
      ? null
      : (versions.find((version) => version.id === selected) ?? null);

  const handleAdd = async () => {
    if (!id || nameConflict) return;
    if (alreadyAdded) {
      navigate('/');
      return;
    }
    if (authConfig?.requireLogin && !isAuthenticated) {
      navigate(`${getRouterBasename().replace(/\/$/, '')}/login`);
      return;
    }
    setAdding(true);
    try {
      if (selectedVersion) {
        await addSharedSkill(id, selectedVersion.id);
      } else {
        await addSharedSkill(id);
      }
      setAdded(true);
      toast.success(t('chat.skills.share.added'));
    } catch (error) {
      const code = promptErrorCode(error);
      if (code === 'already_added') {
        setAlreadyAdded(true);
      } else if (code === 'name_taken') {
        setNameConflict(true);
      } else {
        toast.error(t('chat.skills.share.addError'));
      }
    } finally {
      setAdding(false);
    }
  };

  const addLabel = alreadyAdded
    ? t('chat.skills.share.openCopy')
    : adding
      ? t('chat.skills.share.adding')
      : selectedVersion
        ? t('chat.skills.share.addVersion', {
            version: selectedVersion.version
          })
        : t('chat.skills.share.add');

  return (
    <Page>
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center gap-6 p-6">
        {loading ? (
          <p className="text-sm text-muted-foreground">
            {t('common.status.loading')}
          </p>
        ) : notFound ? (
          <div className="flex flex-col items-center gap-3 text-center">
            <p className="text-sm text-destructive">
              {t('chat.skills.share.notFound')}
            </p>
            <Button variant="outline" onClick={() => navigate('/')}>
              {t('chat.skills.share.goHome')}
            </Button>
          </div>
        ) : skill ? (
          <div className="flex w-full flex-col gap-4">
            <div className="flex flex-col gap-1">
              <h1 className="text-xl font-semibold">{skill.title}</h1>
              <p className="text-sm text-muted-foreground">
                {skill.description}
              </p>
              <p className="text-xs text-muted-foreground">
                {t('chat.skills.share.by', { author: skill.authorLabel })}
                {' · '}
                {t('chat.skills.share.versionLabel', {
                  version: skill.version
                })}
              </p>
            </div>

            {sharingEnabled &&
            versions.some(
              (version) => version.id !== skill.currentVersionId
            ) ? (
              <div className="flex flex-col gap-1">
                <label
                  htmlFor="skill-share-version"
                  className="text-xs text-muted-foreground"
                >
                  {t('chat.skills.share.version')}
                </label>
                <select
                  id="skill-share-version"
                  value={selected}
                  onChange={(event) => setSelected(event.target.value)}
                  className="h-9 rounded-md border bg-background px-2 text-sm"
                >
                  <option value={LATEST}>
                    {t('chat.skills.share.latestVersion', {
                      version: skill.version
                    })}
                  </option>
                  {versions
                    .filter((version) => version.id !== skill.currentVersionId)
                    .map((version) => (
                      <option key={version.id} value={version.id}>
                        {`v${version.version}`}
                      </option>
                    ))}
                </select>
              </div>
            ) : null}

            <div className="max-h-[50vh] overflow-y-auto rounded-md border bg-muted/30 px-4 py-3 custom-scrollbar">
              {previewLoading ? (
                <p className="text-sm text-muted-foreground">…</p>
              ) : (
                <Markdown
                  allowHtml={false}
                  latex={config?.features?.latex}
                  className="prose-sm max-w-none"
                >
                  {previewContent}
                </Markdown>
              )}
            </div>

            {libraryEnabled ? (
              <div className="flex flex-col gap-2">
                <div
                  className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400"
                  data-testid="skill-share-disabled-note"
                >
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{t('chat.skills.share.disabledNote')}</span>
                </div>
                {alreadyAdded ? (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="skill-share-already-added"
                  >
                    {t('chat.skills.share.alreadyAdded')}
                  </p>
                ) : null}
                {nameConflict ? (
                  <p
                    className="text-xs text-destructive"
                    role="alert"
                    data-testid="skill-share-name-conflict"
                  >
                    {t('chat.skills.share.nameTaken')}
                  </p>
                ) : null}
                {added ? (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="skill-share-added"
                  >
                    {t('chat.skills.share.addedReview')}
                  </p>
                ) : null}
                <Button
                  onClick={() => (added ? navigate('/') : handleAdd())}
                  disabled={adding || nameConflict}
                  className="self-start"
                  data-testid="skill-share-add"
                >
                  {added ? t('chat.skills.share.openLibrary') : addLabel}
                </Button>
              </div>
            ) : (
              <p className="flex items-center gap-2 text-xs text-muted-foreground">
                <GlobeLock className="h-4 w-4" />
                {t('chat.skills.share.unavailable')}
              </p>
            )}
          </div>
        ) : null}
      </div>
    </Page>
  );
}
