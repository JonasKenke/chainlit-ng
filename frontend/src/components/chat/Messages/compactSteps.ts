import type { IStep } from '@chainlit/react-client';

export type CotMode = 'hidden' | 'tool_call' | 'full';

const isMessageStep = (step: IStep) => step.type.includes('message');

export const isVisibleStep = (step: IStep, cot: CotMode): boolean => {
  if (cot === 'hidden' || isMessageStep(step)) return false;
  return cot !== 'tool_call' || step.type === 'tool';
};

/** Visible steps in render order, recursing into nested steps. */
export const collectVisibleSteps = (steps: IStep[], cot: CotMode): IStep[] => {
  const visible: IStep[] = [];
  for (const step of steps) {
    if (isVisibleStep(step, cot)) visible.push(step);
    if (step.steps?.length) {
      visible.push(...collectVisibleSteps(step.steps, cot));
    }
  }
  return visible;
};

export const countVisibleSteps = (steps: IStep[], cot: CotMode): number =>
  collectVisibleSteps(steps, cot).length;

/** Assistant/user/system messages at any depth, in render order. */
export const collectMessages = (steps: IStep[]): IStep[] => {
  const messages: IStep[] = [];
  for (const step of steps) {
    if (isMessageStep(step)) messages.push(step);
    else if (step.steps?.length) messages.push(...collectMessages(step.steps));
  }
  return messages;
};

/** Drop messages at every depth without mutating the step tree. */
export const stripMessages = (steps: IStep[]): IStep[] =>
  steps
    .filter((step) => !isMessageStep(step))
    .map((step) =>
      step.steps?.length ? { ...step, steps: stripMessages(step.steps) } : step
    );

/** Last currently executing visible step, if any. */
export const findLastActiveStep = (
  steps: IStep[],
  cot: CotMode
): IStep | undefined => {
  let active: IStep | undefined;
  for (const step of steps) {
    if (isVisibleStep(step, cot) && step.start && !step.end && !step.isError) {
      active = step;
    }
    if (step.steps?.length) {
      active = findLastActiveStep(step.steps, cot) ?? active;
    }
  }
  return active;
};

export const hasErrorStep = (steps: IStep[]): boolean =>
  steps.some(
    (step) =>
      (!isMessageStep(step) && !!step.isError) ||
      (!!step.steps?.length && hasErrorStep(step.steps))
  );

export const getUsedLabelKey = (
  cot: CotMode
): 'chat.messages.status.usedSteps' | 'chat.messages.status.usedTools' =>
  cot === 'tool_call'
    ? 'chat.messages.status.usedTools'
    : 'chat.messages.status.usedSteps';
