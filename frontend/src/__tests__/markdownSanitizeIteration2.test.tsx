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

describe('raw Markdown HTML style sanitizer', () => {
  it('removes style tags and imported stylesheets', () => {
    const { container } = renderRaw(
      '<style>@import url("https://attacker.invalid/stole.css");</style><p>safe</p>'
    );

    expect(container.querySelector('style')).toBeNull();
    expect(container.innerHTML).not.toContain('@import');
    expect(container.innerHTML).not.toContain('attacker.invalid');
    expect(container.textContent).toContain('safe');
  });

  it('removes global overlay CSS', () => {
    const { container } = renderRaw(
      '<style>body::after { content: ""; position: fixed; inset: 0; background: black; }</style><p>safe</p>'
    );

    expect(container.querySelector('style')).toBeNull();
    expect(container.innerHTML).not.toContain('body::after');
    expect(container.innerHTML).not.toContain('position: fixed');
    expect(container.innerHTML).not.toContain('inset: 0');
  });

  it('removes style tags nested inside SVG', () => {
    const { container } = renderRaw(
      '<svg><style>@import url("https://attacker.invalid/svg.css");</style><text>safe</text></svg>'
    );

    expect(container.querySelector('style')).toBeNull();
    expect(container.innerHTML).not.toContain('@import');
    expect(container.innerHTML).not.toContain('attacker.invalid');
    expect(container.querySelector('svg')?.textContent).toContain('safe');
  });

  it('preserves inline style attributes and safe formatting', () => {
    const { container } = renderRaw(
      '<p style="color: red">safe <strong>bold</strong> and <em>emphasis</em></p>'
    );

    expect(
      container.querySelector('[role="article"]')?.getAttribute('style')
    ).toBe('color: red;');
    expect(container.querySelector('.font-bold')?.textContent).toBe('bold');
    expect(container.querySelector('.italic')?.textContent).toBe('emphasis');
  });
});
