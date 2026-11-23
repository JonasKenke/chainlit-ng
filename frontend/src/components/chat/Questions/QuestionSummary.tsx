import { Pencil } from 'lucide-react';
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
      className={`mt-3 grid items-center gap-x-4 gap-y-1.5 text-xs leading-4 ${columns}`}
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
              size="icon"
              className="h-6 w-6 text-muted-foreground hover:text-foreground"
              aria-label={`${t('chat.question.edit')} ${row.prompt}`}
              title={`${t('chat.question.edit')} ${row.prompt}`}
              onClick={() => onEdit(row.key)}
            >
              <Pencil />
            </Button>
          ) : null}
        </Fragment>
      ))}
    </div>
  );
};
