import { beforeEach, describe, expect, it } from 'vitest';

import { clearPersistedUserEnv, createAppStateStore } from '@/state/store';

describe('user environment storage', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('removes persisted API keys on logout', () => {
    const store = createAppStateStore();
    store.getState().setUserEnv({ OPENAI_API_KEY: 'secret' });
    expect(localStorage.getItem('userEnv')).toContain('secret');

    clearPersistedUserEnv();

    expect(localStorage.getItem('userEnv')).toBeNull();
  });
});
