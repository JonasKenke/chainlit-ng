import { Fragment } from 'react';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

export interface QuestionSummaryRow {
  key: string;
  prompt: string;
  answer: string | null;
  muted?: boolean;
}

interface Props {
  rows: QuestionSummaryRow[];
  emptyLabel: string;
  onEdit?: (key: string) => void;
}

export const QuestionSummary = ({ rows, emptyLabel, onEdit }: Props) => {
  const { t } = useTranslation();
  const columns = onEdit
    ? 'grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_auto]'
    : 'grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]';

  return (
    <div
      data-testid="question-summary"
      className={`mt-3 grid gap-x-4 gap-y-2 text-xs ${columns}`}
    >
      {rows.map((row) => (
        <Fragment key={row.key}>
          <div className="min-w-0 text-muted-foreground">
            <bdi dir="auto">{row.prompt}</bdi>
          </div>
          <div
            className={`min-w-0 text-right ${
              row.muted
                ? 'text-muted-foreground'
                : 'font-medium text-foreground'
            }`}
          >
            <bdi dir="auto" className="whitespace-pre-wrap break-words">
              {row.answer ?? emptyLabel}
            </bdi>
          </div>
          {onEdit ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-1.5 text-xs font-normal text-muted-foreground hover:text-foreground"
              aria-label={`${t('chat.question.edit')} ${row.prompt}`}
              onClick={() => onEdit(row.key)}
            >
              {t('chat.question.edit')}
            </Button>
          ) : null}
        </Fragment>
      ))}
    </div>
  );
};
