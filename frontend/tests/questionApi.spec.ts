import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChainlitAPI } from '@chainlit/react-client';

describe('respondToQuestion', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('returns the terminal record carried by a 409', async () => {
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    const terminal = {
      questionId: 'question-1',
      status: 'superseded',
      answer: null,
      revision: 2,
      durable: false,
      error: 'QuestionNotPending'
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(terminal), {
          status: 409,
          headers: { 'Content-Type': 'application/json' }
        })
      )
    );

    await expect(
      client.respondToQuestion({
        sessionId: 'session-1',
        questionId: 'question-1',
        text: 'late',
        decisionRequestId: 'request-1'
      })
    ).resolves.toEqual(terminal);
  });
});
