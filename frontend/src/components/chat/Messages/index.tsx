import {
  type IAction,
  type IMessageElement,
  type IStep
} from '@chainlit-ng/react-client';
import { MessageContext } from 'contexts/MessageContext';
import React, { memo, useContext, useMemo } from 'react';

import BlinkingCursor from '@/components/BlinkingCursor';

import { CompactSteps } from './CompactSteps';
import { Message } from './Message';
import { StepInteractions } from './StepInteractions';
import { collectMessages, countVisibleSteps } from './compactSteps';

interface Props {
  messages: IStep[];
  elements: IMessageElement[];
  actions: IAction[];
  indent: number;
  isRunning?: boolean;
  scorableRun?: IStep;
}

const CL_RUN_NAMES = ['on_chat_start', 'on_message', 'on_audio_end'];

const hasActiveToolStep = (step: IStep): boolean => {
  return (
    step.steps?.some(
      (s) =>
        (s.type === 'tool' && s.start && !s.end && !s.isError) ||
        s.type.includes('message') ||
        hasActiveToolStep(s)
    ) || false
  );
};

const hasAssistantMessage = (step: IStep): boolean => {
  return (
    step.steps?.some(
      (s) => s.type === 'assistant_message' || hasAssistantMessage(s)
    ) || false
  );
};

const Messages = memo(
  ({ messages, elements, actions, indent, isRunning, scorableRun }: Props) => {
    const messageContext = useContext(MessageContext);

    const lastAssistantMessage = useMemo(() => {
      return messages.findLast((m) => m.type === 'assistant_message');
    }, [messages]);

    const lastScorableAssistantMessage = useMemo(() => {
      return scorableRun?.steps?.findLast(
        (m) => m.type === 'assistant_message'
      );
    }, [scorableRun]);

    return (
      <>
        {messages.map((m) => {
          // Handle chainlit runs
          if (CL_RUN_NAMES.includes(m.name)) {
            const isRunning = !m.end && !m.isError && messageContext.loading;
            const isToolCallCoT =
              messageContext.cot === 'tool_call' ||
              messageContext.cot === 'full';
            const isHiddenCoT = messageContext.cot === 'hidden';

            const showToolCoTLoader = isToolCallCoT
              ? isRunning && !hasActiveToolStep(m)
              : false;

            const showHiddenCoTLoader = isHiddenCoT
              ? isRunning && !hasAssistantMessage(m)
              : false;
            // Ignore on_chat_start for scorable run
            const scorableRun =
              !isRunning && m.name !== 'on_chat_start' ? m : undefined;

            const useCompact =
              messageContext.cotDisplay === 'compact' &&
              messageContext.cot !== 'hidden';
            const showCompact =
              useCompact &&
              countVisibleSteps(m.steps || [], messageContext.cot) > 1;

            return (
              <React.Fragment key={m.id}>
                <StepInteractions stepId={m.id} threadId={m.threadId} />
                {m.steps?.length ? (
                  showCompact ? (
                    <>
                      <CompactSteps
                        steps={m.steps}
                        elements={elements}
                        actions={actions}
                        indent={indent}
                        isRunning={isRunning}
                        scorableRun={scorableRun}
                      />
                      {/* Messages at any depth stay visible at the root. */}
                      <Messages
                        messages={collectMessages(m.steps)}
                        elements={elements}
                        actions={actions}
                        indent={indent}
                        isRunning={isRunning}
                        scorableRun={scorableRun}
                      />
                    </>
                  ) : (
                    <Messages
                      messages={m.steps}
                      elements={elements}
                      actions={actions}
                      indent={indent}
                      isRunning={isRunning}
                      scorableRun={scorableRun}
                    />
                  )
                ) : null}
                {(showToolCoTLoader || showHiddenCoTLoader) &&
                m.name !== 'on_chat_start' ? (
                  <BlinkingCursor />
                ) : null}
              </React.Fragment>
            );
          } else {
            // Score the current run
            const _scorableRun = m.type === 'run' ? m : scorableRun;
            // The message is scorable if it is the last assistant message of the run

            const isRunLastAssistantMessage =
              m.type === 'run' ? false : m === lastScorableAssistantMessage;

            const isLastAssistantMessage = m === lastAssistantMessage;

            const isScorable =
              isRunLastAssistantMessage || isLastAssistantMessage;

            return (
              <Message
                message={m}
                elements={elements}
                actions={actions}
                key={m.id}
                indent={indent}
                isRunning={isRunning}
                scorableRun={_scorableRun}
                isScorable={isScorable}
              />
            );
          }
        })}
      </>
    );
  }
);

export { Messages };
