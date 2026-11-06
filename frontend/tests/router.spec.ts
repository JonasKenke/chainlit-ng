import { afterEach, describe, expect, it } from 'vitest';

import { getAppUrl } from '../src/lib/router';

let rootPathMeta: HTMLMetaElement | undefined;

const setRootPath = (rootPath?: string) => {
  rootPathMeta?.remove();
  rootPathMeta = undefined;
  if (rootPath === undefined) return;
  rootPathMeta = document.createElement('meta');
  rootPathMeta.setAttribute('property', 'og:root_path');
  rootPathMeta.setAttribute('content', rootPath);
  document.head.appendChild(rootPathMeta);
};

describe('getAppUrl', () => {
  afterEach(() => setRootPath());

  it('builds a share link without a root path', () => {
    expect(getAppUrl('/share/abc')).toBe(`${window.location.origin}/share/abc`);
  });

  it('treats a root path of / as no root path', () => {
    setRootPath('/');
    expect(getAppUrl('/share/abc')).toBe(`${window.location.origin}/share/abc`);
  });

  it('prefixes the configured root path', () => {
    setRootPath('/app');
    expect(getAppUrl('/share/abc')).toBe(
      `${window.location.origin}/app/share/abc`
    );
  });

  it('normalizes a trailing slash on the root path', () => {
    setRootPath('/apps/internal/');
    expect(getAppUrl('/share/abc')).toBe(
      `${window.location.origin}/apps/internal/share/abc`
    );
  });

  it('joins a relative path without doubling the slash', () => {
    setRootPath('/app');
    expect(getAppUrl('share/abc')).toBe(
      `${window.location.origin}/app/share/abc`
    );
  });

  it('leaves absolute URLs untouched', () => {
    setRootPath('/app');
    expect(getAppUrl('https://cdn.example.com/share/abc')).toBe(
      'https://cdn.example.com/share/abc'
    );
  });
});
