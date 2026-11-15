import {
  useChatData,
  useChatInteract,
  useChatMessages
} from '@chainlit-ng/react-client';

import { Send } from '@/components/icons/Send';
import { Stop } from '@/components/icons/Stop';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';
import { Translator, useTranslation } from 'components/i18n';

interface SubmitButtonProps {
  disabled?: boolean;
  onSubmit: () => void;
}

export default function SubmitButton({
  disabled,
  onSubmit
}: SubmitButtonProps) {
  const { loading } = useChatData();
  const { firstInteraction } = useChatMessages();
  const { stopTask } = useChatInteract();
  const { t } = useTranslation();

  return (
    <TooltipProvider>
      {loading && firstInteraction ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="stop-button"
              aria-label={t('chat.input.actions.stop', {
                defaultValue: 'Stop Task'
              })}
              onClick={stopTask}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Stop className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.stop" />
            </p>
          </TooltipContent>
        </Tooltip>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="chat-submit"
              aria-label={t('chat.input.actions.send', {
                defaultValue: 'Send message'
              })}
              disabled={disabled}
              onClick={onSubmit}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Send className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.send" />
            </p>
          </TooltipContent>
        </Tooltip>
      )}
    </TooltipProvider>
  );
}
