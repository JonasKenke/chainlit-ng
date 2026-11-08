import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageContext } from 'contexts/MessageContext';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChainlitContext } from '@chainlit/react-client';

import { ApprovalCard } from '@/components/chat/Approvals/ApprovalCard';

const respondToApproval = vi.fn();

vi.mock('@chainlit/react-client', async () => {
  const ReactModule = await import('react');
  return {
    ChainlitContext: ReactModule.createContext({}),
    useChainlitStore: (selector: (state: any) => unknown) =>
      selector({ sessionId: 'session-1' }),
    useSetChainlitState: () => vi.fn()
  };
});

const approval = {
  id: 'approval-1',
  stepId: 'step-1',
  threadId: 'thread-1',
  tool: 'send_email',
  title: 'Send the draft',
  status: 'pending' as const,
  arguments: {
    to: 'person@example.com',
    apiKey: 'server-held-secret'
  }
};

function renderCard() {
  return render(
    <ChainlitContext.Provider value={{ respondToApproval } as any}>
      <MessageContext.Provider value={{ cot: 'hidden' } as any}>
        <ApprovalCard approval={approval} />
      </MessageContext.Provider>
    </ChainlitContext.Provider>
  );
}

describe('ApprovalCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders with cot hidden and shows the exact server arguments', () => {
    renderCard();

    expect(screen.getByTestId('approval-card')).toBeInTheDocument();
    expect(screen.getByText('send_email')).toBeInTheDocument();
    expect(screen.getByText('Send the draft')).toBeInTheDocument();
    expect(screen.getByText(/person@example.com/)).toBeInTheDocument();
    expect(screen.getByText(/server-held-secret/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('sends a decision without client-held arguments', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'approved',
      decision: 'approve',
      revision: 2
    });
    renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    const payload = respondToApproval.mock.calls[0][0];
    expect(payload).toMatchObject({
      sessionId: 'session-1',
      approvalId: 'approval-1',
      decision: 'approve'
    });
    expect(payload).not.toHaveProperty('arguments');
    expect(payload.decisionRequestId).toBeTruthy();
  });

  it('does not offer buttons for a terminal approval', () => {
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard
          approval={{
            ...approval,
            status: 'approved',
            decidedBy: 'test_user_identifier',
            decidedAt: '2026-01-01T00:00:00Z'
          }}
        />
      </ChainlitContext.Provider>
    );

    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull();
    expect(screen.getByText('Approved')).toBeInTheDocument();
  });
});
