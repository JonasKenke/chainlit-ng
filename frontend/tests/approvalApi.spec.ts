import { ChainlitAPI, ClientError } from '@chainlit-ng/react-client';
import { afterEach, describe, expect, it, vi } from 'vitest';

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

describe('ClientError', () => {
  it('renders a structured 422 detail as field and type', () => {
    const error = new ClientError(
      'Unprocessable Entity',
      422,
      {
        error: 'ApprovalInvalidArguments',
        field: 'editedArguments.unknownField',
        type: 'extra_forbidden'
      } as any,
      {}
    );

    expect(error.toString()).toContain('editedArguments.unknownField');
    expect(error.toString()).toContain('extra_forbidden');
    expect(error.toString()).not.toContain('[object Object]');
  });
});
