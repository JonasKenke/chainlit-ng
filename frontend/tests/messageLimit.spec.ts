import { isMessageTooLarge } from '@/lib/utils';
import { describe, expect, it } from 'vitest';

const userMessage = (output: string) => ({
  threadId: '',
  id: 'message-id',
  name: 'alice',
  type: 'user_message' as const,
  output,
  createdAt: '2026-11-08T00:00:00.000Z',
  metadata: { location: 'http://localhost:8000/' }
});

describe('isMessageTooLarge', () => {
  it('accepts a message below the Engine.IO 1 MB frame limit', () => {
    expect(isMessageTooLarge(userMessage('a'.repeat(900_000)))).toBe(false);
  });

  it('rejects a message above the Engine.IO 1 MB frame limit', () => {
    expect(isMessageTooLarge(userMessage('a'.repeat(1_200_000)))).toBe(true);
  });

  it('counts UTF-8 bytes rather than characters', () => {
    // 1.04 MB as UTF-8, but only 520k characters.
    expect(isMessageTooLarge(userMessage('😀'.repeat(260_000)))).toBe(true);
  });

  it('counts file references in the frame', () => {
    expect(
      isMessageTooLarge(userMessage('a'.repeat(900_000)), [
        { id: 'b'.repeat(200_000) }
      ])
    ).toBe(true);
  });
});
