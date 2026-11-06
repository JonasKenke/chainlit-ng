import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const pdfjsMock = vi.hoisted(() => ({
  GlobalWorkerOptions: { workerSrc: '' }
}));

vi.mock('react-pdf', () => ({
  pdfjs: pdfjsMock,
  Document: () => null,
  Page: () => null
}));

const setRootPath = (rootPath: string) => {
  document.head.innerHTML = `<meta property="og:root_path" content="${rootPath}" />`;
};

describe('PDF worker URL', () => {
  beforeEach(() => {
    vi.resetModules();
    pdfjsMock.GlobalWorkerOptions.workerSrc = '';
  });

  afterEach(() => {
    document.head.innerHTML = '';
  });

  it('prefixes the worker URL with the runtime app root', async () => {
    setRootPath('/spec_bot');

    await import('@/components/Elements/PDF');

    expect(pdfjsMock.GlobalWorkerOptions.workerSrc).toBe(
      `/spec_bot${workerUrl}`
    );
  });

  it('keeps the worker URL unchanged without a root path', async () => {
    setRootPath('');

    await import('@/components/Elements/PDF');

    expect(pdfjsMock.GlobalWorkerOptions.workerSrc).toBe(workerUrl);
  });
});
