import { describe, expect, it } from 'vitest';

import type { IStep } from '@chainlit/react-client';

import {
  collectMessages,
  collectVisibleSteps,
  countVisibleSteps,
  findLastActiveStep,
  getUsedLabelKey,
  hasErrorStep,
  stripMessages
} from '@/components/chat/Messages/compactSteps';

const step = (
  id: string,
  type: IStep['type'],
  extra: Partial<IStep> = {}
): IStep =>
  ({
    id,
    name: id,
    type,
    output: '',
    createdAt: 0,
    ...extra
  }) as IStep;

describe('countVisibleSteps', () => {
  it('counts non-message steps recursively in full mode', () => {
    const steps = [
      step('tool1', 'tool'),
      step('assistant', 'assistant_message'),
      step('agent', 'undefined', {
        steps: [
          step('tool2', 'tool'),
          step('user', 'user_message'),
          step('nested', 'llm')
        ]
      })
    ];

    expect(countVisibleSteps(steps, 'full')).toBe(4);
  });

  it('counts only tools recursively in tool_call mode', () => {
    const steps = [
      step('tool1', 'tool'),
      step('agent', 'undefined', {
        steps: [step('tool2', 'tool'), step('llm', 'llm')]
      })
    ];

    expect(collectVisibleSteps(steps, 'tool_call').map((s) => s.id)).toEqual([
      'tool1',
      'tool2'
    ]);
  });

  it('has no visible steps when CoT is hidden', () => {
    expect(countVisibleSteps([step('tool1', 'tool')], 'hidden')).toBe(0);
  });
});

describe('collectMessages', () => {
  it('collects messages at any depth and ignores steps', () => {
    const steps = [
      step('tool1', 'tool'),
      step('agent', 'undefined', {
        steps: [
          step('nested_answer', 'assistant_message', {
            steps: [step('deep_answer', 'assistant_message')]
          }),
          step('tool2', 'tool')
        ]
      }),
      step('answer', 'assistant_message')
    ];

    expect(collectMessages(steps).map((s) => s.id)).toEqual([
      'nested_answer',
      'answer'
    ]);
  });
});

describe('stripMessages', () => {
  it('removes messages at every depth without mutating the tree', () => {
    const nested = step('agent', 'undefined', {
      steps: [step('tool1', 'tool'), step('nested_answer', 'assistant_message')]
    });
    const steps = [nested, step('answer', 'assistant_message')];

    const stripped = stripMessages(steps);

    expect(stripped.map((s) => s.id)).toEqual(['agent']);
    expect(stripped[0].steps?.map((s) => s.id)).toEqual(['tool1']);
    expect(nested.steps).toHaveLength(2);
    expect(steps).toHaveLength(2);
  });
});

describe('findLastActiveStep', () => {
  it('returns the deepest last running visible step', () => {
    const steps = [
      step('tool1', 'tool', { start: '1', end: '2' }),
      step('agent', 'undefined', {
        start: '1',
        steps: [
          step('tool2', 'tool', { start: '1' }),
          step('tool3', 'tool', { start: '1' })
        ]
      })
    ];

    expect(findLastActiveStep(steps, 'full')?.id).toBe('tool3');
  });

  it('falls back to the parent when only it is active', () => {
    const steps = [
      step('agent', 'undefined', {
        start: '1',
        steps: [step('tool1', 'tool', { start: '1', end: '2' })]
      })
    ];

    expect(findLastActiveStep(steps, 'full')?.id).toBe('agent');
  });

  it('ignores active steps that are not visible in tool_call mode', () => {
    const steps = [
      step('agent', 'undefined', { start: '1' }),
      step('tool1', 'tool', { start: '1', end: '2' })
    ];

    expect(findLastActiveStep(steps, 'tool_call')).toBeUndefined();
  });
});

describe('hasErrorStep', () => {
  it('detects errors nested inside steps', () => {
    const steps = [
      step('agent', 'undefined', {
        steps: [step('tool1', 'tool', { isError: true })]
      })
    ];

    expect(hasErrorStep(steps)).toBe(true);
  });

  it('ignores errors on message steps', () => {
    expect(
      hasErrorStep([step('answer', 'assistant_message', { isError: true })])
    ).toBe(false);
  });
});

describe('getUsedLabelKey', () => {
  it('uses tool labels only in tool_call mode', () => {
    expect(getUsedLabelKey('tool_call')).toBe('chat.messages.status.usedTools');
    expect(getUsedLabelKey('full')).toBe('chat.messages.status.usedSteps');
  });
});
