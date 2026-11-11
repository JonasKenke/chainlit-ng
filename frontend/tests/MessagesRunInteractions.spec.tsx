import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Messages } from '@/components/chat/Messages';

vi.mock('@/components/chat/Messages/Message', () => ({
  Message: () => <div data-testid="message-marker" />
}));

vi.mock('@/components/chat/Messages/StepInteractions', () => ({
  StepInteractions: ({ stepId }: { stepId: string }) =>
    stepId === 'run-1' ? <div data-testid="interaction-marker" /> : null
}));

const runStep = {
  id: 'run-1',
  name: 'on_message',
  type: 'run' as const,
  threadId: 'thread-1',
  output: '',
  createdAt: '2026-11-09T00:00:00Z',
  steps: [
    {
      id: 'answer-1',
      name: 'assistant',
      type: 'assistant_message' as const,
      output: 'done',
      createdAt: '2026-11-09T00:00:01Z'
    }
  ]
};

describe('run-step interactions', () => {
  it('renders the interaction before the run child messages', () => {
    render(
      <Messages messages={[runStep]} elements={[]} actions={[]} indent={0} />
    );

    const interaction = screen.getByTestId('interaction-marker');
    const message = screen.getByTestId('message-marker');
    expect(
      interaction.compareDocumentPosition(message) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });
});
