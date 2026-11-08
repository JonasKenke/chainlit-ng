import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChainlitAPI } from '@chainlit/react-client';

describe('respondToApproval', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('returns the terminal record carried by a 409', async () => {
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    const terminal = {
      approvalId: 'approval-1',
      status: 'superseded',
      revision: 2,
      terminalReason: 'stopped'
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
      client.respondToApproval({
        sessionId: 'session-1',
        approvalId: 'approval-1',
        decision: 'approve',
        decisionRequestId: 'request-1'
      })
    ).resolves.toEqual(terminal);
  });
});
