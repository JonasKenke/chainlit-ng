import { useTranslation } from '@/components/i18n';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  contentId?: string;
  confirmId?: string;
  title?: string;
  description?: string;
  confirmLabel?: string;
};

/**
 * Shared confirmation shown before a chat change discards a live approval or
 * question. Cancel keeps the current chat and card untouched.
 */
export default function PendingInteractionDialog({
  open,
  onOpenChange,
  onConfirm,
  contentId,
  confirmId,
  title,
  description,
  confirmLabel
}: Props) {
  const { t } = useTranslation();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent id={contentId} className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {title ??
              t('navigation.pendingInteraction.title', {
                defaultValue: 'Pending approval or question'
              })}
          </DialogTitle>
          <DialogDescription>
            {description ??
              t('navigation.pendingInteraction.description', {
                defaultValue:
                  'This chat still has an approval or question waiting. Changing chats will discard it.'
              })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t('common.actions.cancel', { defaultValue: 'Cancel' })}
          </Button>
          <Button variant="default" onClick={onConfirm} id={confirmId}>
            {confirmLabel ??
              t('navigation.pendingInteraction.confirm', {
                defaultValue: 'Switch chat'
              })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
