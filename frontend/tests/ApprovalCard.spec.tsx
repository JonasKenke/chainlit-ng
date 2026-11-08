import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageContext } from 'contexts/MessageContext';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChainlitContext } from '@chainlit/react-client';

import { ApprovalCard } from '@/components/chat/Approvals/ApprovalCard';

const respondToApproval = vi.fn();
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

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

  it('offers Edit only when the server allows it', () => {
    renderCard();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();

    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard approval={{ ...approval, editAllowed: true }} />
      </ChainlitContext.Provider>
    );
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  it('sends a schema-validated edit to the server', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'edited',
      decision: 'edit',
      revision: 2
    });
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard approval={{ ...approval, editAllowed: true }} />
      </ChainlitContext.Provider>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Edited arguments JSON'), {
      target: { value: '{"to": "other@example.com"}' }
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }));

    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    const payload = respondToApproval.mock.calls[0][0];
    expect(payload).toMatchObject({
      sessionId: 'session-1',
      approvalId: 'approval-1',
      decision: 'edit',
      editedArguments: { to: 'other@example.com' }
    });
  });

  it('does not send invalid JSON', () => {
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard approval={{ ...approval, editAllowed: true }} />
      </ChainlitContext.Provider>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Edited arguments JSON'), {
      target: { value: '{not json' }
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }));

    expect(respondToApproval).not.toHaveBeenCalled();
  });

  it('shows the final arguments and an original-to-edited diff', () => {
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard
          approval={{
            ...approval,
            status: 'edited',
            decision: 'edit',
            arguments: { to: 'other@example.com', apiKey: 'sk-edited' },
            argumentsPreview: {
              to: 'person@example.com',
              apiKey: '[redacted]'
            },
            editedArgumentsPreview: {
              to: 'other@example.com',
              apiKey: '[redacted]'
            },
            decidedBy: 'test_user_identifier',
            decidedAt: '2026-01-01T00:00:00Z'
          }}
        />
      </ChainlitContext.Provider>
    );

    expect(screen.getByText('Edited')).toBeInTheDocument();
    expect(screen.getAllByText(/other@example.com/).length).toBeGreaterThan(0);
    const diff = screen.getByTestId('approval-diff');
    expect(diff).toHaveTextContent(
      '~ to: "person@example.com" → "other@example.com"'
    );
    expect(diff).not.toHaveTextContent('apiKey');
  });

  it('shows the field path and error type for an invalid edit', async () => {
    respondToApproval.mockRejectedValueOnce({
      message: 'Unprocessable Entity',
      payload: {
        detail: {
          error: 'ApprovalInvalidArguments',
          field: 'editedArguments.unknownField',
          type: 'extra_forbidden'
        }
      }
    });
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard approval={{ ...approval, editAllowed: true }} />
      </ChainlitContext.Provider>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Edited arguments JSON'), {
      target: { value: '{"to": "other@example.com"}' }
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }));

    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const message = String(toastError.mock.calls.at(-1)?.[0]);
    expect(message).toContain('editedArguments.unknownField');
    expect(message).toContain('extra_forbidden');
    expect(message).not.toContain('[object Object]');
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
