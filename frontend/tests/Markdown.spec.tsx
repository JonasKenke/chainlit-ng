import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Markdown } from 'components/Markdown';

vi.mock('@/components/i18n/Translator', () => ({
  default: () => null,
  useTranslation: () => ({ t: (key: string) => key, ready: true, i18n: {} })
}));

const renderMarkdown = (children: string, latex?: boolean) =>
  render(<Markdown latex={latex}>{children}</Markdown>);

describe('Markdown math', () => {
  it('leaves TeX delimiters and dollar math literal when latex is false', () => {
    const { container } = renderMarkdown(
      'Ohm: \\( R = V / I \\) and $e^{i\\pi}+1=0$',
      false
    );

    expect(container.querySelector('.katex')).toBeNull();
    expect(container.textContent).toContain('( R = V / I )');
    expect(container.textContent).toContain('$e^{i\\pi}+1=0$');
  });

  it('renders \\(...\\) inline math when latex is true', () => {
    const { container } = renderMarkdown('Ohm: \\( R = V / I \\) done', true);

    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.querySelector('.katex-display')).toBeNull();
    expect(container.textContent).not.toContain('\\(');
    expect(container.textContent).not.toContain('\\)');
  });

  it('renders \\[...\\] display math at the start of a line', () => {
    const { container } = renderMarkdown('\\[ y = ax^2 + bx + c \\]', true);

    expect(container.querySelector('.katex-display')).not.toBeNull();
    expect(container.textContent).not.toContain('\\[');
    expect(container.textContent).not.toContain('\\]');
  });

  it('keeps dollar math rendering unchanged', () => {
    const inline = renderMarkdown('$e^{i\\pi}+1=0$', true);
    expect(inline.container.querySelector('.katex')).not.toBeNull();
    expect(inline.container.querySelector('.katex-display')).toBeNull();
    inline.unmount();

    const display = renderMarkdown('$$\ny = ax^2\n$$', true);
    expect(display.container.querySelector('.katex-display')).not.toBeNull();
  });

  it('does not parse emphasis inside delimiters', () => {
    const inline = renderMarkdown('\\(a*b\\)', true);
    expect(inline.container.querySelector('.katex')).not.toBeNull();
    expect(inline.container.querySelector('em')).toBeNull();
    inline.unmount();

    const display = renderMarkdown('\\[c_d\\]', true);
    expect(display.container.querySelector('.katex-display')).not.toBeNull();
    expect(display.container.querySelector('em')).toBeNull();
  });

  it('keeps inline code and fenced code literal', () => {
    const inline = renderMarkdown('`\\(x\\)`', true);
    expect(inline.container.querySelector('.katex')).toBeNull();
    expect(inline.container.querySelector('code')?.textContent).toBe('\\(x\\)');
    inline.unmount();

    const fenced = renderMarkdown('```\n\\[y\\]\n```', true);
    expect(fenced.container.querySelector('.katex')).toBeNull();
    expect(fenced.container.textContent).toContain('\\[y\\]');
  });

  it('keeps an unclosed delimiter as text until it is closed', () => {
    const { container, rerender } = renderMarkdown('Solving \\( x + 1', true);

    expect(container.querySelector('.katex')).toBeNull();
    expect(container.textContent).toContain('( x + 1');

    rerender(<Markdown latex>{'Solving \\( x + 1 \\)'}</Markdown>);

    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.textContent).not.toContain('\\(');
  });

  it('falls back to text when the closing delimiter is beyond the lookahead', () => {
    const { container } = renderMarkdown(`\\(${'x'.repeat(2000)}\\)`, true);

    expect(container.querySelector('.katex')).toBeNull();
  });

  it('bounds the parse cost of repeated unclosed delimiters', () => {
    const payload = '\\(x'.repeat(Math.floor((32 * 1024) / 3));
    const started = performance.now();
    const { container } = renderMarkdown(payload, true);
    const elapsed = performance.now() - started;

    expect(container.querySelector('.katex')).toBeNull();
    expect(elapsed).toBeLessThan(20000);
  }, 60000);
});
