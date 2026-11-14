import { type ChainlitAPI, getChainlitStore } from '@chainlit-ng/react-client';
import { act, renderHook } from '@testing-library/react';
import type { PropsWithChildren } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  AppStateProvider,
  createAppStateStore,
  useAppStore
} from '../src/state/store';

afterEach(() => localStorage.clear());

describe('Zustand state', () => {
  it('scopes chat state to each Chainlit API instance', () => {
    const first = getChainlitStore({} as ChainlitAPI);
    const second = getChainlitStore({} as ChainlitAPI);

    first.setState({ firstUserInteraction: 'user' });

    expect(first.getState().firstUserInteraction).toBe('user');
    expect(second.getState().firstUserInteraction).toBeUndefined();
  });

  it('provides isolated app state to hooks', () => {
    const store = createAppStateStore({ chatSettingsOpen: true });
    const wrapper = ({ children }: PropsWithChildren) => (
      <AppStateProvider store={store}>{children}</AppStateProvider>
    );
    const { result } = renderHook(
      () => useAppStore((state) => state.chatSettingsOpen),
      {
        wrapper
      }
    );

    act(() => store.getState().setChatSettingsOpen(false));
    expect(result.current).toBe(false);
  });

  it('persists user environment updates', () => {
    const store = createAppStateStore({ userEnv: { API_KEY: 'old' } });
    store.getState().setUserEnv((previous) => ({
      ...previous,
      API_KEY: 'new'
    }));

    expect(JSON.parse(localStorage.getItem('userEnv')!)).toEqual({
      API_KEY: 'new'
    });
  });
});
