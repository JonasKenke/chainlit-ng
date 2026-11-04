import { useChainlitState, useSetChainlitState } from 'src/state';

export const useAuthState = () => {
  const [authConfig, setAuthConfig] = useChainlitState('auth');
  const [user, setUser] = useChainlitState('user');
  const setThreadHistory = useSetChainlitState('threadHistory');

  return {
    authConfig,
    setAuthConfig,
    user,
    setUser,
    setThreadHistory
  };
};
