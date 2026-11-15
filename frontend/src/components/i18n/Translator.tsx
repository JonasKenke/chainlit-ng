import { TOptions } from 'i18next';
import { useCallback } from 'react';
import { useTranslation as usei18nextTranslation } from 'react-i18next';

import { Skeleton } from '@/components/ui/skeleton';

type options = TOptions<Record<string, unknown>>;

type TranslatorProps = {
  path: string | string[];
  suffix?: string;
  options?: options;
};

const Translator = ({ path, options, suffix }: TranslatorProps) => {
  const { t, i18n } = usei18nextTranslation();

  if (!i18n.exists(path, options)) {
    return <Skeleton className="h-4 w-10" />;
  }

  return (
    <span>
      {t(path, options)}
      {suffix}
    </span>
  );
};

export const useTranslation = () => {
  const { t, ready, i18n } = usei18nextTranslation();

  const translate = useCallback(
    (path: string | string[], options?: options) => {
      // i18n.exists() ignores defaultValue, so call t() when a fallback is
      // provided instead of returning the missing-translation placeholder.
      if (options?.defaultValue !== undefined) {
        return t(path, options);
      }

      if (!i18n.exists(path, options)) {
        return '...';
      }

      return t(path, options);
    },
    [i18n, t]
  );

  return {
    t: translate,
    ready,
    i18n
  };
};

export default Translator;
