import { type ChainlitAPI, ChainlitContext } from '@chainlit-ng/react-client';
import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Markdown } from '@/components/Markdown';

vi.mock('@/components/i18n/Translator', () => ({
  default: () => null,
  useTranslation: () => ({ t: (key: string) => key, ready: true, i18n: {} })
}));

const apiClient = {
  buildEndpoint: (path: string) => `http://localhost:8000${path}`
} as unknown as ChainlitAPI;

function renderRaw(payload: string) {
  return render(
    <ChainlitContext.Provider value={apiClient}>
      <Markdown allowHtml>{payload}</Markdown>
    </ChainlitContext.Provider>
  );
}

describe('raw Markdown HTML sanitizer', () => {
  it('removes active containers instead of executing them', () => {
    const { container } = renderRaw(
      [
        '<iframe srcdoc="<script>parent.pwned=1</script>"></iframe>',
        '<object data="https://attacker.invalid/x"></object>',
        '<embed src="https://attacker.invalid/x">',
        '<form action="https://attacker.invalid/collect"><input></form>',
        '<script>globalThis.pwned=1</script>',
        '<p>safe <strong>text</strong></p>'
      ].join('')
    );

    expect(
      container.querySelector('iframe,object,embed,form,script')
    ).toBeNull();
    expect(container.textContent).toContain('safe');
    expect(container.textContent).toContain('text');
  });

  it('strips event handlers, srcdoc and executable URLs', () => {
    const { container } = renderRaw(
      [
        '<img src="x" onerror="globalThis.pwned=1">',
        '<a href="javascript:globalThis.pwned=1">click</a>',
        '<iframe srcdoc="still blocked"></iframe>'
      ].join('')
    );

    expect(container.querySelector('iframe')).toBeNull();
    const img = container.querySelector('img');
    expect(img?.getAttribute('onerror')).toBeNull();
    const link = container.querySelector('a');
    expect(
      link?.getAttribute('href')?.toLowerCase().startsWith('javascript:')
    ).not.toBe(true);
  });
});
