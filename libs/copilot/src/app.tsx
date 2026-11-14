import { ChainlitContext, useAuth } from '@chainlit-ng/react-client';
import { useContext, useEffect, useState } from 'react';
import { Toaster } from 'sonner';
import { IWidgetConfig } from 'types';
import Widget from 'widget';

import { useTranslation } from '@chainlit/app/src/components/i18n/Translator';

import { useCopilotInteract } from './hooks/useCopilotInteract';

import { ThemeProvider } from './ThemeProvider';
import {
  COPILOT_THREAD_CHANGED_EVENT_KEY,
  CopilotThreadChangedEventParams
} from './state';

interface Props {
  widgetConfig: IWidgetConfig;
}

const BLOCKED_COOKIE_ERROR =
  'Authentication failed. This browser may be blocking third-party cookies. Serve Chainlit on the same site as this page (recommended), or allow third-party cookies for this site.';

declare global {
  interface Window {
    cl_shadowRootElement: HTMLDivElement;
    toggleChainlitCopilot: () => void;
    theme?: {
      light: Record<string, string>;
      dark: Record<string, string>;
    };
    getChainlitCopilotThreadId: () => string | null;
    clearChainlitCopilotThreadId: (newThreadId?: string) => void;
  }
}

export default function App({ widgetConfig }: Props) {
  const { isAuthenticated, data, setUserFromAPI } = useAuth();
  const apiClient = useContext(ChainlitContext);
  const { i18n } = useTranslation();
  const { startNewChat } = useCopilotInteract();
  const languageInUse = widgetConfig.language || navigator.language || 'en-US';
  const [authError, setAuthError] = useState<string>();
  const [fetchError, setFetchError] = useState<string>();

  useEffect(() => {
    apiClient
      .get(`/project/translations?language=${languageInUse}`)
      .then((res) => res.json())
      .then((data) => {
        i18n.addResourceBundle(languageInUse, 'translation', data.translation);
        i18n.changeLanguage(languageInUse);
      })
      .catch((err) => {
        setFetchError(String(err));
      });
  }, []);

  const defaultTheme = widgetConfig.theme || data?.default_theme;

  useEffect(() => {
    if (fetchError) return;
    if (!isAuthenticated) {
      if (!widgetConfig.accessToken) {
        setAuthError('No authentication token provided.');
      } else {
        apiClient
          .jwtAuth(widgetConfig.accessToken)
          .then(async () => {
            // A dropped Set-Cookie still resolves jwtAuth; only /user can detect it.
            const res = await fetch(apiClient.buildEndpoint('/user'), {
              credentials: 'include'
            });
            if (res.status === 401) {
              setAuthError(BLOCKED_COOKIE_ERROR);
            } else if (!res.ok) {
              setAuthError(`${res.status} ${res.statusText}`);
            } else {
              await setUserFromAPI();
            }
          })
          .catch((err) => setAuthError(String(err)));
      }
    } else {
      setAuthError(undefined);
    }
  }, [isAuthenticated, apiClient, fetchError, setAuthError]);

  useEffect(() => {
    const eventListener = (e: Event) => {
      const customEvent = e as CustomEvent<CopilotThreadChangedEventParams>;
      startNewChat(customEvent?.detail?.newThreadId);
    };

    window.addEventListener(COPILOT_THREAD_CHANGED_EVENT_KEY, eventListener);

    return () => {
      window.removeEventListener(
        COPILOT_THREAD_CHANGED_EVENT_KEY,
        eventListener
      );
    };
  }, []);

  return (
    <ThemeProvider storageKey="vite-ui-theme" defaultTheme={defaultTheme}>
      <Toaster className="toast" position="bottom-center" />
      <Widget config={widgetConfig} error={fetchError || authError} />
    </ThemeProvider>
  );
}
