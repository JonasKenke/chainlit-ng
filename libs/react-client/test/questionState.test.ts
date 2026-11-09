// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import type { IQuestion } from '../src/types/question';

import type { ChainlitAPI } from '../src/api';
import { getChainlitStore } from '../src/state';

const question: IQuestion = {
  kind: 'question',
  id: 'question-1',
  stepId: 'step-1',
  threadId: 'thread-1',
  status: 'pending',
  prompt: 'Which environment?',
  options: [{ id: 'staging', label: 'Staging' }],
  allowFreeText: true,
  live: true
};

describe('question state', () => {
  it('starts empty and stores questions per client', () => {
    const first = getChainlitStore({} as ChainlitAPI);
    const second = getChainlitStore({} as ChainlitAPI);

    expect(first.getState().questions).toEqual([]);
    first.setState({ questions: [question] });

    expect(first.getState().questions[0].id).toBe('question-1');
    expect(second.getState().questions).toEqual([]);
  });
});
