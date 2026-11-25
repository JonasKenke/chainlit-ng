import { isTrustedMessageOrigin } from '@chainlit-ng/react-client';
import { describe, expect, it } from 'vitest';

const APP = 'https://app.example';

describe('window message origin policy', () => {
  it('accepts the page origin', () => {
    expect(isTrustedMessageOrigin(APP, APP, [])).toBe(true);
  });

  it('accepts an explicitly configured peer', () => {
    expect(
      isTrustedMessageOrigin('https://embed.example', APP, [
        'https://embed.example'
      ])
    ).toBe(true);
  });

  it('does not treat a wildcard as a peer', () => {
    expect(isTrustedMessageOrigin('https://evil.example', APP, ['*'])).toBe(
      false
    );
  });

  it('rejects an unlisted origin and an empty origin', () => {
    expect(isTrustedMessageOrigin('https://evil.example', APP, [])).toBe(false);
    expect(isTrustedMessageOrigin('', APP, ['https://embed.example'])).toBe(
      false
    );
    expect(isTrustedMessageOrigin('null', APP, [])).toBe(false);
  });
});
