import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useTranslation } from '@/components/i18n/Translator';

const { t, exists } = vi.hoisted(() => ({
  t: vi.fn(
    (_key: string, options?: { defaultValue?: string }) => options?.defaultValue
  ),
  exists: vi.fn()
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t, i18n: { exists }, ready: true })
}));

describe('useTranslation', () => {
  it('uses the defaultValue when the active locale lacks the key', () => {
    exists.mockReturnValue(false);

    const { result } = renderHook(() => useTranslation());

    expect(
      result.current.t('chat.input.errors.tooLarge', {
        defaultValue: 'Message is too large.'
      })
    ).toBe('Message is too large.');
  });

  it('keeps the placeholder when the key is missing without a default', () => {
    exists.mockReturnValue(false);

    const { result } = renderHook(() => useTranslation());

    expect(result.current.t('missing.key')).toBe('...');
  });
});
