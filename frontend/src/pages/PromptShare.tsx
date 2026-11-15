import getRouterBasename from '@/lib/router';
import {
  ChainlitContext,
  IPromptVersionSummary,
  ISharedPrompt,
  promptErrorCode,
  useAuth,
  useConfig,
  usePromptGallery
} from '@chainlit-ng/react-client';
import { useContext, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import Page from 'pages/Page';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

const LATEST = 'latest';

export default function PromptSharePage() {
  const { id } = useParams<{ id: string }>();
  const apiClient = useContext(ChainlitContext);
  const { config } = useConfig();
  const { data: authConfig, isAuthenticated } = useAuth();
  const { addSharedPrompt } = usePromptGallery();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [prompt, setPrompt] = useState<ISharedPrompt | null>(null);
  const [versions, setVersions] = useState<IPromptVersionSummary[]>([]);
  const [selected, setSelected] = useState<string>(LATEST);
  const [previewContent, setPreviewContent] = useState<string>('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState(false);
  const [alreadyAdded, setAlreadyAdded] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const versioningEnabled =
    config?.promptGallery !== false && config?.promptVersioning === true;

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setLoading(true);
    apiClient
      .getSharedPrompt(id)
      .then((p) => {
        if (cancelled) return;
        setPrompt(p);
        setPreviewContent(p.content);
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
  }, [id, apiClient]);

  // Version history is optional: a data layer without versioning answers 501,
  // in which case the selector is hidden rather than shown empty.
  useEffect(() => {
    if (!id || !versioningEnabled) return;
    let cancelled = false;
    apiClient
      .fetchPromptVersions(id, { shared: true })
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
  }, [id, apiClient, versioningEnabled]);

  useEffect(() => {
    if (!id || selected === LATEST) {
      setPreviewContent(prompt?.content ?? '');
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    apiClient
      .fetchPromptVersion(id, selected, { shared: true })
      .then((version) => {
        if (!cancelled) setPreviewContent(version.content);
      })
      .catch(() => {
        if (!cancelled) setPreviewContent(prompt?.content ?? '');
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, apiClient, selected, prompt?.content]);

  const basename = getRouterBasename().replace(/\/$/, '');
  const loginPath = `${basename}/login`;

  const selectedVersion =
    selected === LATEST
      ? null
      : (versions.find((version) => version.id === selected) ?? null);

  const handleAdd = async () => {
    if (!id) return;
    if (alreadyAdded) {
      navigate('/');
      return;
    }
    if (authConfig?.requireLogin && !isAuthenticated) {
      navigate(loginPath);
      return;
    }
    setAdding(true);
    try {
      if (selectedVersion) {
        await addSharedPrompt(id, selectedVersion.id);
      } else {
        await addSharedPrompt(id);
      }
      setAdded(true);
      toast.success(t('chat.promptGallery.added'));
      navigate('/');
    } catch (err) {
      if (promptErrorCode(err) === 'already_added') {
        setAlreadyAdded(true);
        return;
      }
      toast.error(t('chat.promptGallery.share.addError'));
    } finally {
      setAdding(false);
    }
  };

  const addLabel = alreadyAdded
    ? t('chat.promptGallery.share.openCopy')
    : adding
      ? t('chat.promptGallery.share.adding')
      : selectedVersion
        ? t('chat.promptGallery.share.addVersion', {
            version: selectedVersion.version
          })
        : t('chat.promptGallery.addToGallery');

  return (
    <Page>
      <div className="flex flex-col items-center justify-center flex-1 p-6 gap-6 max-w-xl mx-auto w-full">
        {loading ? (
          <p className="text-muted-foreground text-sm">
            {t('common.status.loading')}
          </p>
        ) : notFound ? (
          <div className="flex flex-col items-center gap-3 text-center">
            <p className="text-destructive text-sm">
              {t('chat.promptGallery.share.notFound')}
            </p>
            <Button variant="outline" onClick={() => navigate('/')}>
              {t('chat.promptGallery.share.goHome')}
            </Button>
          </div>
        ) : prompt ? (
          <div className="w-full flex flex-col gap-4">
            <h1 className="text-xl font-semibold">{prompt.title}</h1>
            {versioningEnabled &&
            versions.some(
              (version) => version.id !== prompt.currentVersionId
            ) ? (
              <div className="flex flex-col gap-1">
                <label
                  htmlFor="prompt-share-version"
                  className="text-xs text-muted-foreground"
                >
                  {t('chat.promptGallery.share.version')}
                </label>
                <select
                  id="prompt-share-version"
                  value={selected}
                  onChange={(event) => setSelected(event.target.value)}
                  className="h-9 rounded-md border bg-background px-2 text-sm"
                >
                  <option value={LATEST}>
                    {t('chat.promptGallery.share.latestVersion', {
                      version: prompt.version
                    })}
                  </option>
                  {versions
                    .filter((version) => version.id !== prompt.currentVersionId)
                    .map((version) => (
                      <option key={version.id} value={version.id}>
                        {`v${version.version}`}
                      </option>
                    ))}
                </select>
              </div>
            ) : null}
            <div className="rounded-md border bg-muted/30 px-4 py-3">
              <pre
                className="whitespace-pre-wrap text-sm font-sans"
                data-testid="prompt-share-content"
              >
                {previewLoading ? '…' : previewContent}
              </pre>
            </div>
            {config?.promptGallery !== false ? (
              <Button
                onClick={handleAdd}
                disabled={adding || added}
                className="self-start"
                data-testid="prompt-share-add"
              >
                {added ? t('chat.promptGallery.added') : addLabel}
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </Page>
  );
}
