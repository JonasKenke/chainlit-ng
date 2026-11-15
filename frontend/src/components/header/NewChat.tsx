import { useChatInteract, useConfig } from '@chainlit-ng/react-client';
import React, { useState } from 'react';

import PendingInteractionDialog from '@/components/PendingInteractionDialog';
import { Translator, useTranslation } from '@/components/i18n';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';

import { useHasPendingInteraction } from '@/hooks/usePendingInteraction';

import { EditSquare } from '../icons/EditSquare';

type NewChatDialogProps = {
  open: boolean;
  handleClose: () => void;
  handleConfirm: () => void;
};

export const NewChatDialog = ({
  open,
  handleClose,
  handleConfirm
}: NewChatDialogProps) => {
  const handleKeyDown = (event: React.KeyboardEvent) => {
    event.preventDefault();
    if (event.key === 'Enter') {
      handleConfirm();
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent
        id="new-chat-dialog"
        className="sm:max-w-md"
        onKeyDown={handleKeyDown}
      >
        <DialogHeader>
          <DialogTitle>
            <Translator path="navigation.newChat.dialog.title" />
          </DialogTitle>
          <DialogDescription>
            <Translator path="navigation.newChat.dialog.description" />
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={handleClose}>
            <Translator path="common.actions.cancel" />
          </Button>
          <Button variant="default" onClick={handleConfirm} id="confirm">
            <Translator path="common.actions.confirm" />
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

interface Props extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  navigate?: (to: string) => void;
  onConfirm?: () => void;
}

const NewChatButton = ({ navigate, onConfirm, ...buttonProps }: Props) => {
  const [open, setOpen] = useState(false);
  const [pendingWarningOpen, setPendingWarningOpen] = useState(false);
  const { clear } = useChatInteract();
  const { config } = useConfig();
  const { t } = useTranslation();
  const hasPendingInteraction = useHasPendingInteraction();

  const handleClickOpen = () => {
    if (hasPendingInteraction) {
      setPendingWarningOpen(true);
      return;
    }
    if (config?.ui?.confirm_new_chat === false) {
      handleConfirm();
    } else {
      setOpen(true);
    }
  };

  const handleClose = () => {
    setOpen(false);
  };

  const handleConfirm = () => {
    if (onConfirm) {
      onConfirm();
    } else {
      clear();
      navigate?.('/');
    }
    setPendingWarningOpen(false);
    handleClose();
  };

  return (
    <div>
      <PendingInteractionDialog
        open={pendingWarningOpen}
        onOpenChange={setPendingWarningOpen}
        onConfirm={handleConfirm}
        contentId="pending-new-chat-dialog"
        confirmId="confirm-pending-new-chat"
        title={t('navigation.newChat.pending.title', {
          defaultValue: 'Pending approval or question'
        })}
        description={t('navigation.newChat.pending.description', {
          defaultValue:
            'This chat still has an approval or question waiting. Starting a new chat will discard it.'
        })}
        confirmLabel={t('navigation.newChat.pending.confirm', {
          defaultValue: 'Start new chat'
        })}
      />
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              id="new-chat-button"
              className="text-muted-foreground hover:text-muted-foreground"
              onClick={handleClickOpen}
              {...buttonProps}
            >
              <EditSquare className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <Translator path="navigation.newChat.dialog.tooltip" />
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <NewChatDialog
        open={open}
        handleClose={handleClose}
        handleConfirm={handleConfirm}
      />
    </div>
  );
};

export default NewChatButton;
