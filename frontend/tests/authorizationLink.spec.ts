import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getAuthorizationLink,
  openAuthorizationUrl
} from '@/components/chat/MessageComposer/Mcp/authorizationLink';

describe('MCP authorization link', () => {
  afterEach(() => vi.restoreAllMocks());

  it('accepts http(s) and keeps the full host', () => {
    expect(
      getAuthorizationLink('https://auth.example.com/authorize?state=abc')
    ).toEqual({
      href: 'https://auth.example.com/authorize?state=abc',
      host: 'auth.example.com'
    });
    expect(getAuthorizationLink('http://localhost:9000/authorize')).toEqual({
      href: 'http://localhost:9000/authorize',
      host: 'localhost:9000'
    });
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'file:///etc/passwd',
    'not a url',
    ''
  ])('refuses %j', (url) => {
    expect(getAuthorizationLink(url)).toBeNull();
    expect(openAuthorizationUrl(url)).toBe(false);
  });

  it('opens on demand with noopener,noreferrer', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);

    expect(openAuthorizationUrl('https://auth.example.com/authorize')).toBe(
      true
    );
    expect(open).toHaveBeenCalledWith(
      'https://auth.example.com/authorize',
      '_blank',
      'noopener,noreferrer'
    );
  });
});
