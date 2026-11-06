import { render, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import CodeSnippet from 'components/CodeSnippet';

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({ t: (key: string) => key })
}));

const codeBlock = (language: string, code: string) => (
  <CodeSnippet
    node={{
      children: [
        {
          properties: { className: [`language-${language}`] },
          children: [{ value: code }]
        }
      ]
    }}
  />
);

const waitForHighlight = (code: Element, selector: string) =>
  waitFor(() => expect(code.querySelector(selector)).toBeInTheDocument(), {
    timeout: 15000
  });

it('renders code immediately and highlights it after highlight.js loads', async () => {
  const { container } = render(codeBlock('python', 'print("hello")'));
  const code = container.querySelector('code')!;

  expect(code.textContent).toBe('print("hello")');
  expect(code).toHaveClass('hljs');
  expect(code.querySelector('.hljs-built_in')).not.toBeInTheDocument();

  await waitForHighlight(code, '.hljs-built_in');
});

it('highlights an uncommon language after highlight.js loads', async () => {
  const { container } = render(
    codeBlock('fortran', 'program hello\n  print *, "hello"\nend program hello')
  );
  const code = container.querySelector('code')!;

  await waitForHighlight(code, '.hljs-keyword');
});

it('falls back to plaintext for an unknown language', async () => {
  const { container } = render(codeBlock('not-a-language', 'const x = 1;'));
  const code = container.querySelector('code')!;

  await waitFor(() => expect(code).toHaveClass('language-txt'), {
    timeout: 15000
  });

  expect(code.textContent).toBe('const x = 1;');
  expect(code.querySelector('.hljs-keyword')).not.toBeInTheDocument();
});

it('ignores a stale highlight when the code changes before the chunk loads', async () => {
  const { container, rerender } = render(codeBlock('python', 'old = 1'));
  rerender(codeBlock('python', 'new = 2'));
  const code = container.querySelector('code')!;

  await waitForHighlight(code, '.hljs-number');

  expect(code.textContent).toBe('new = 2');
  expect(code.querySelector('.hljs-number')).toBeInTheDocument();
});
