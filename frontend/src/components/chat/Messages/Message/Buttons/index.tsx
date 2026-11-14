import { IAction, type IStep } from '@chainlit-ng/react-client';

import CopyButton from '@/components/CopyButton';

import MessageActions from './Actions';
import { FeedbackButtons } from './FeedbackButtons';

interface Props {
  message: IStep;
  actions: IAction[];
  run?: IStep;
  contentRef?: React.RefObject<HTMLDivElement | null>;
}

const MessageButtons = ({ message, actions, run, contentRef }: Props) => {
  const isUser = message.type === 'user_message';
  const isAsk = message.waitForAnswer;
  const hasContent = !!message.output;
  const showCopyButton = !!run && hasContent && !isUser && !isAsk;

  const messageActions = actions.filter((a) => a.forId === message.id);

  const show = showCopyButton || messageActions.length > 0;

  if (!show || message.streaming) {
    return null;
  }

  return (
    <div className="-ml-1.5 flex items-center flex-wrap">
      {showCopyButton ? (
        <CopyButton content={message.output} contentRef={contentRef} />
      ) : null}
      {run ? <FeedbackButtons message={run} /> : null}
      {messageActions.length ? (
        <MessageActions actions={messageActions} />
      ) : null}
    </div>
  );
};

export { MessageButtons };
