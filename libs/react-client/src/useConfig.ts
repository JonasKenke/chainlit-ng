import { useEffect, useRef } from 'react';

import { useApi, useAuth } from './api';
import { useChainlitState, useChainlitStore } from './state';
import { IChainlitConfig } from './types';

const useConfig = () => {
  const [config, setConfig] = useChainlitState('config');
  const { isAuthenticated } = useAuth();
  const chatProfile = useChainlitStore((state) => state.chatProfile);
  const language = navigator.language || 'en-US';
  const prevChatProfileRef = useRef(chatProfile);

  // Build the API URL with optional chat profile parameter
  const apiUrl = isAuthenticated
    ? `/project/settings?language=${language}${chatProfile ? `&chat_profile=${encodeURIComponent(chatProfile)}` : ''}`
    : null;

  // Always fetch if we don't have config and we're authenticated
  const shouldFetch = isAuthenticated && !config;

  const { data, error, isLoading } = useApi<IChainlitConfig>(
    shouldFetch ? apiUrl : null
  );

  useEffect(() => {
    if (!data) return;
    setConfig(data);
  }, [data, setConfig]);

  // Clear config when chat profile changes to force re-fetch
  useEffect(() => {
    if (prevChatProfileRef.current !== chatProfile) {
      setConfig(undefined);
      prevChatProfileRef.current = chatProfile;
    }
  }, [chatProfile, setConfig]);

  return { config, error, isLoading, language };
};

export { useConfig };
