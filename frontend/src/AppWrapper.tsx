import getRouterBasename from '@/lib/router';
import {
  isTrustedMessageOrigin,
  useApi,
  useAuth,
  useChatInteract,
  useConfig
} from '@chainlit-ng/react-client';
import App from 'App';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

export default function AppWrapper() {
  const [translationLoaded, setTranslationLoaded] = useState(false);
  const { isAuthenticated, isReady } = useAuth();
  const { config, language: languageInUse } = useConfig();
  const { i18n } = useTranslation();
  const { windowMessage } = useChatInteract();

  function handleChangeLanguage(languageBundle: any): void {
    i18n.addResourceBundle(languageInUse, 'translation', languageBundle);
    i18n.changeLanguage(languageInUse);
  }

  const { data: translations } = useApi<any>(
    `/project/translations?language=${languageInUse}`
  );

  useEffect(() => {
    if (!translations) return;
    handleChangeLanguage(translations.translation);
    setTranslationLoaded(true);
  }, [translations]);

  useEffect(() => {
    const handleWindowMessage = (event: MessageEvent) => {
      // Only this origin or an explicitly configured peer may drive the
      // app-level window camera. A wildcard CORS posture must not turn every
      // embedding page into a trusted message source.
      if (
        !isTrustedMessageOrigin(
          event.origin,
          window.location.origin,
          config?.allowOrigins
        )
      ) {
        return;
      }
      windowMessage(event.data);
    };
    window.addEventListener('message', handleWindowMessage);
    return () => window.removeEventListener('message', handleWindowMessage);
  }, [config?.allowOrigins, windowMessage]);

  if (!translationLoaded) return null;

  if (
    isReady &&
    !isAuthenticated &&
    window.location.pathname !== getRouterBasename() + '/login' &&
    window.location.pathname !== getRouterBasename() + '/login/callback'
  ) {
    window.location.href = getRouterBasename() + '/login';
  }
  return <App />;
}
