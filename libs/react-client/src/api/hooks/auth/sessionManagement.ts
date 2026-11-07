import { useContext } from 'react';
import { ChainlitContext } from 'src/index';
import { useResetChainlitState } from 'src/state';

import { useAuthState } from './state';

export const useSessionManagement = () => {
  const apiClient = useContext(ChainlitContext);
  const { setUser, setThreadHistory } = useAuthState();
  const resetMcps = useResetChainlitState('mcps');

  const logout = async (reload = false): Promise<void> => {
    // Drop persisted MCP credentials before the network call so a failed
    // logout cannot leave them at rest on a shared browser.
    resetMcps();
    await apiClient.logout();
    setUser(undefined);
    setThreadHistory(undefined);

    if (reload) {
      window.location.reload();
    }
  };

  return { logout };
};
