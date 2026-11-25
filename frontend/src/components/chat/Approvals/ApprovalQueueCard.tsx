import type { IApproval } from '@chainlit-ng/react-client';

import { useTranslation } from '@/components/i18n/Translator';

import { ApprovalCard } from './ApprovalCard';

interface Props {
  approvals: IApproval[];
}

/** One review surface when several tool calls are waiting at the same time. */
export function ApprovalQueueCard({ approvals }: Props) {
  const { t } = useTranslation();

  return (
    <section
      data-testid="approval-queue-card"
      className="scroll-mt-4 rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="text-sm font-semibold">
        {t('chat.approval.group.title', {
          count: approvals.length,
          defaultValue: '{{count}} tools need approval'
        })}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {t('chat.approval.group.description', {
          defaultValue:
            'Review each call. Decisions apply to that tool independently.'
        })}
      </p>
      <div className="mt-3 flex max-h-[min(70vh,calc(100dvh_-_18rem))] flex-col gap-3 overflow-y-auto custom-scrollbar pr-1">
        {approvals.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval} grouped />
        ))}
      </div>
    </section>
  );
}

export default ApprovalQueueCard;
