import type { IApproval, IApprovalEditField } from '@chainlit-ng/react-client';
import { ChainlitContext } from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageContext } from 'contexts/MessageContext';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApprovalCard } from '@/components/chat/Approvals/ApprovalCard';

const respondToApproval = vi.fn();
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));

vi.mock('sonner', () => ({ toast: { error: toastError } }));

vi.mock('@chainlit-ng/react-client', async () => {
  const ReactModule = await import('react');
  return {
    ChainlitContext: ReactModule.createContext({}),
    useChainlitStore: (selector: (state: any) => unknown) =>
      selector({ sessionId: 'session-1' }),
    useSetChainlitState: () => vi.fn()
  };
});

const approval: IApproval = {
  id: 'approval-1',
  stepId: 'step-1',
  threadId: 'thread-1',
  tool: 'send_email',
  title: 'Send the draft',
  status: 'pending',
  arguments: {
    to: 'person@example.com',
    apiKey: 'server-held-secret'
  }
};

function renderCard(overrides: Partial<IApproval> = {}) {
  return render(
    <ChainlitContext.Provider value={{ respondToApproval } as any}>
      <MessageContext.Provider value={{ cot: 'hidden' } as any}>
        <ApprovalCard approval={{ ...approval, ...overrides }} />
      </MessageContext.Provider>
    </ChainlitContext.Provider>
  );
}

describe('ApprovalCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the review body and actions without raw arguments', () => {
    renderCard();

    expect(screen.getByTestId('approval-card')).toBeInTheDocument();
    expect(screen.getByText('Send Email')).toBeInTheDocument();
    expect(screen.queryByText('send_email')).toBeNull();
    expect(screen.getByText('Send the draft')).toBeInTheDocument();
    expect(screen.queryByText('Awaiting decision')).toBeNull();
    expect(screen.queryByText(/person@example.com/)).toBeNull();
    expect(screen.queryByText(/server-held-secret/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('humanizes camelCase tool names', () => {
    renderCard({ tool: 'createTicket' });

    expect(screen.getByText('Create Ticket')).toBeInTheDocument();
    expect(screen.queryByText('createTicket')).toBeNull();
  });

  it('renders markdown content without exposing the JSON arguments', () => {
    renderCard({
      content: '| change | rows |\n| --- | --- |\n| approvals | 3 |',
      arguments: { rows: 3 }
    });

    expect(screen.getByTestId('approval-content')).toBeInTheDocument();
    expect(screen.getByText('approvals')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-arguments')).toBeNull();
    expect(screen.queryByText(/"rows": 3/)).toBeNull();
  });

  it('renders only the actions when there are no arguments', () => {
    const { container } = renderCard({ arguments: {} });

    expect(container.querySelector('pre')).toBeNull();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('renders the title as text, never as HTML', () => {
    const title = '<img src=x onerror="alert(1)">';
    const { container } = renderCard({ title });

    expect(screen.getByText(title)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
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

  const editFields: IApprovalEditField[] = [
    {
      name: 'to',
      label: 'To',
      kind: 'string',
      required: true,
      nullable: false,
      secret: false
    },
    {
      name: 'count',
      label: 'Count',
      kind: 'integer',
      required: false,
      nullable: true,
      secret: false
    },
    {
      name: 'labels',
      label: 'Labels',
      kind: 'string_list',
      required: false,
      nullable: false,
      secret: false
    },
    {
      name: 'urgent',
      label: 'Urgent',
      kind: 'boolean',
      required: false,
      nullable: false,
      secret: false
    }
  ];

  it('submits typed values from the generated edit form', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'edited',
      decision: 'edit',
      revision: 2
    });
    renderCard({
      editAllowed: true,
      editFields,
      arguments: {
        to: 'person@example.com',
        count: 2,
        labels: ['api'],
        urgent: false
      }
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('To')).toHaveValue('person@example.com');

    fireEvent.change(screen.getByLabelText('To'), {
      target: { value: 'other@example.com' }
    });
    fireEvent.change(screen.getByLabelText('Count'), {
      target: { value: '3' }
    });
    fireEvent.change(screen.getByLabelText('Labels'), {
      target: { value: 'api\nreliability' }
    });
    fireEvent.click(screen.getByLabelText('Urgent'));
    fireEvent.click(screen.getByRole('button', { name: 'Save & approve' }));

    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    expect(respondToApproval.mock.calls[0][0]).toMatchObject({
      approvalId: 'approval-1',
      decision: 'edit',
      editedArguments: {
        to: 'other@example.com',
        count: 3,
        labels: ['api', 'reliability'],
        urgent: true
      }
    });
  });

  it('shows the generated edit form behind a divider and heading', () => {
    renderCard({ editAllowed: true, editFields, arguments: { to: 'a@b.c' } });

    expect(screen.queryByText('Edit fields')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    const form = screen.getByTestId('approval-edit-form');
    expect(form).toHaveClass('border-t', 'pt-3');
    expect(screen.getByText('Edit fields')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Save & approve' })
    ).toBeInTheDocument();
  });

  it('keeps the JSON editor and no edit-fields heading without edit fields', () => {
    renderCard({ editAllowed: true, arguments: { to: 'a@b.c' } });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.queryByText('Edit fields')).toBeNull();
    expect(screen.queryByTestId('approval-edit-form')).toBeNull();
    expect(screen.getByLabelText('Edited arguments JSON')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Save edit' })
    ).toBeInTheDocument();
  });

  it('renders embedded newlines in diff values as real line breaks', () => {
    renderCard({
      status: 'edited',
      decision: 'edit',
      argumentsPreview: { body: 'first line\nsecond line' },
      editedArgumentsPreview: {
        body: 'first line\nsecond line\nthird line'
      }
    });

    const diff = screen.getByTestId('approval-diff');
    expect(diff.querySelector('pre')).not.toBeNull();
    const text = diff.textContent ?? '';
    expect(text).toContain('"first line\nsecond line"');
    expect(text).toContain('second line\nthird line"');
    expect(text).not.toContain('\\n');
  });

  it('reports an invalid number field', () => {
    renderCard({
      editAllowed: true,
      editFields: [
        {
          name: 'amount',
          label: 'Amount',
          kind: 'number',
          required: false,
          nullable: false,
          secret: false
        }
      ],
      arguments: { amount: 'not-a-number' }
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save & approve' }));

    expect(respondToApproval).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith('Amount must be a number');
  });

  it('reports an invalid integer field', () => {
    renderCard({
      editAllowed: true,
      editFields: [
        {
          name: 'count',
          label: 'Count',
          kind: 'integer',
          required: false,
          nullable: false,
          secret: false
        }
      ],
      arguments: { count: 1 }
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Count'), {
      target: { value: '3.5' }
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save & approve' }));

    expect(respondToApproval).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith('Count must be a whole number');
  });

  it('renders enum options and submits the selected value', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'edited',
      decision: 'edit',
      revision: 2
    });
    renderCard({
      editAllowed: true,
      editFields: [
        {
          name: 'priority',
          label: 'Priority',
          kind: 'enum',
          required: true,
          nullable: false,
          secret: false,
          options: ['low', 'high']
        }
      ],
      arguments: { priority: 'low' }
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const select = screen.getByLabelText('Priority');
    expect(select).toHaveValue('low');
    expect(screen.getByRole('option', { name: 'low' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'high' })).toBeInTheDocument();

    fireEvent.change(select, { target: { value: 'high' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & approve' }));

    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    expect(respondToApproval.mock.calls[0][0]).toMatchObject({
      approvalId: 'approval-1',
      decision: 'edit',
      editedArguments: { priority: 'high' }
    });
  });

  it('renders multiline and secret fields with their own input types', () => {
    renderCard({
      editAllowed: true,
      editFields: [
        {
          name: 'body',
          label: 'Body',
          kind: 'text',
          required: false,
          nullable: true,
          secret: false
        },
        {
          name: 'token',
          label: 'Token',
          kind: 'string',
          required: false,
          nullable: true,
          secret: true
        }
      ],
      arguments: { body: 'hello', token: 'shh' }
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    const body = screen.getByLabelText('Body');
    expect(body.tagName).toBe('TEXTAREA');
    expect(body).toHaveValue('hello');
    expect(screen.getByLabelText('Token')).toHaveAttribute('type', 'password');
  });

  it('hides editable fields when the approval is no longer live', () => {
    renderCard({ editAllowed: true, editFields, live: false });

    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('rejects immediately without opening a comment box', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'rejected',
      decision: 'reject',
      revision: 2
    });
    renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));

    expect(screen.queryByLabelText('Comment')).toBeNull();
    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    const payload = respondToApproval.mock.calls[0][0];
    expect(payload).toMatchObject({
      approvalId: 'approval-1',
      decision: 'reject'
    });
    expect(payload).not.toHaveProperty('reason');
  });

  it('requires and sends a comment when requesting changes', async () => {
    respondToApproval.mockResolvedValue({
      approvalId: 'approval-1',
      status: 'changes_requested',
      decision: 'request_changes',
      reason: 'split the ticket',
      revision: 2
    });
    renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Request changes' }));
    const submit = screen.getByRole('button', { name: 'Request changes' });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Comment'), {
      target: { value: 'split the ticket' }
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(respondToApproval).toHaveBeenCalledTimes(1));
    expect(respondToApproval.mock.calls[0][0]).toMatchObject({
      approvalId: 'approval-1',
      decision: 'request_changes',
      reason: 'split the ticket'
    });
  });

  it('shows the persisted comment on a terminal card', () => {
    renderCard({
      status: 'changes_requested',
      decision: 'request_changes',
      reason: 'split the ticket',
      decidedBy: 'test_user_identifier',
      decidedAt: '2026-01-01T00:00:00Z'
    });

    expect(screen.getByText('Changes requested')).toBeInTheDocument();
    expect(screen.getByTestId('approval-comment')).toHaveTextContent(
      'split the ticket'
    );
  });

  it('shows the final arguments and an original-to-edited diff', () => {
    render(
      <ChainlitContext.Provider value={{ respondToApproval } as any}>
        <ApprovalCard
          approval={{
            ...approval,
            status: 'edited',
            decision: 'edit',
            content: 'Requested draft',
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
    expect(
      screen.getByText(
        'Requested content; the approved changes are in the diff.'
      )
    ).toBeInTheDocument();
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
