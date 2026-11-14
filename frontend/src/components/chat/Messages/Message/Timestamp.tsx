import { cn } from '@/lib/utils';
import type { IStep } from '@chainlit-ng/react-client';
import { useConfig } from '@chainlit-ng/react-client';
import { memo, useMemo } from 'react';

interface Props {
  createdAt?: IStep['createdAt'];
  className?: string;
}

export const MessageTimestamp = memo(function MessageTimestamp({
  createdAt,
  className
}: Props) {
  const { config } = useConfig();
  const timestamp = useMemo(() => {
    if (!config?.ui?.show_message_timestamps || createdAt == null) {
      return null;
    }

    const date = new Date(createdAt);
    if (Number.isNaN(date.getTime())) {
      return null;
    }

    return {
      label: new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(
        date
      ),
      title: new Intl.DateTimeFormat(undefined, {
        dateStyle: 'full',
        timeStyle: 'long'
      }).format(date),
      dateTime: date.toISOString()
    };
  }, [config?.ui?.show_message_timestamps, createdAt]);

  if (!timestamp) {
    return null;
  }

  return (
    <time
      dateTime={timestamp.dateTime}
      title={timestamp.title}
      className={cn('text-xs text-muted-foreground', className)}
    >
      {timestamp.label}
    </time>
  );
});
