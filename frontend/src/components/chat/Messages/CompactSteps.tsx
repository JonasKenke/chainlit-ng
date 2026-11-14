import { cn } from '@/lib/utils';
import type {
  IAction,
  IMessageElement,
  IStep
} from '@chainlit-ng/react-client';
import { MessageContext } from 'contexts/MessageContext';
import { memo, useContext, useMemo, useState } from 'react';

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger
} from '@/components/ui/accordion';
import { Translator } from 'components/i18n';
import { useTranslation } from 'components/i18n/Translator';

import { Messages } from '.';
import { MessageAvatar } from './Message/Avatar';
import {
  collectVisibleSteps,
  findLastActiveStep,
  getUsedLabelKey,
  hasErrorStep,
  stripMessages
} from './compactSteps';

interface Props {
  steps: IStep[];
  elements: IMessageElement[];
  actions: IAction[];
  indent: number;
  isRunning?: boolean;
  scorableRun?: IStep;
}

const CompactSteps = memo(
  ({ steps, elements, actions, indent, isRunning, scorableRun }: Props) => {
    const { cot, uiName } = useContext(MessageContext);
    const { t } = useTranslation();
    const [openValue, setOpenValue] = useState<string>('');

    // Messages are lifted to the root by Messages, so strip them at every depth
    // to avoid trapping or duplicating them inside the collapsed accordion.
    const stepChildren = useMemo(() => stripMessages(steps), [steps]);
    const visibleSteps = useMemo(
      () => collectVisibleSteps(steps, cot),
      [steps, cot]
    );
    const activeStep = useMemo(
      () => findLastActiveStep(steps, cot),
      [steps, cot]
    );
    const isError = useMemo(() => hasErrorStep(steps), [steps]);

    const lastVisibleStep = visibleSteps[visibleSteps.length - 1];
    const runningStepName =
      activeStep?.name || lastVisibleStep?.name || uiName || '';

    const usedLabelPath = getUsedLabelKey(cot);
    const accordionId = 'compact-steps';

    return (
      <div className="step py-2" data-testid="compact-steps">
        <div className="flex flex-grow pb-2">
          <div className="ai-message flex gap-4 w-full">
            <MessageAvatar
              author={
                lastVisibleStep?.metadata?.avatarName ||
                lastVisibleStep?.name ||
                'Assistant'
              }
              isError={isError}
              iconName={lastVisibleStep?.metadata?.icon}
            />
            <div className="flex flex-col flex-grow w-0">
              <Accordion
                type="single"
                collapsible
                value={openValue}
                onValueChange={(val) => setOpenValue(val)}
                className="w-full"
              >
                <AccordionItem value={accordionId} className="border-none">
                  <AccordionTrigger
                    data-testid="compact-steps-trigger"
                    className={cn(
                      'flex items-center gap-1 justify-start transition-none p-0 hover:no-underline mt-[3px] rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                      isError && 'text-red-500',
                      !isRunning &&
                        !isError &&
                        'text-muted-foreground hover:text-foreground',
                      isRunning && 'loading-shimmer'
                    )}
                  >
                    {isRunning ? (
                      <>
                        <Translator path="chat.messages.status.using" />{' '}
                        {runningStepName}
                      </>
                    ) : (
                      <Translator
                        path={usedLabelPath}
                        options={{ count: visibleSteps.length }}
                      />
                    )}
                    {isError ? (
                      <span className="sr-only">
                        {t('common.status.error.default')}
                      </span>
                    ) : null}
                  </AccordionTrigger>
                  <AccordionContent>
                    <div className="flex-grow mt-4 ml-1 pl-4 border-l-2 border-primary">
                      <Messages
                        messages={stepChildren}
                        elements={elements}
                        actions={actions}
                        indent={indent + 1}
                        isRunning={isRunning}
                        scorableRun={scorableRun}
                      />
                    </div>
                  </AccordionContent>
                </AccordionItem>
              </Accordion>
            </div>
          </div>
        </div>
      </div>
    );
  }
);

CompactSteps.displayName = 'CompactSteps';

export { CompactSteps };
