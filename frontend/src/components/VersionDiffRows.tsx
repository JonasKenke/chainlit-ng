import type { IPromptDiffRow } from '@chainlit-ng/react-client';

export type VersionDiffViewMode = 'unified' | 'split';

interface Props {
  rows: IPromptDiffRow[];
  viewMode: VersionDiffViewMode;
  /** Row test id; defaults to the prompt dialog's contract. */
  testId?: string;
}

const OP_CLASS: Record<IPromptDiffRow['op'], string> = {
  equal: '',
  insert: 'bg-emerald-500/10',
  delete: 'bg-destructive/10',
  replace: 'bg-amber-500/10'
};

interface DisplayRow {
  op: IPromptDiffRow['op'];
  oldLine: number | null;
  newLine: number | null;
  text: string;
  marker: string;
  tone: 'equal' | 'insert' | 'delete';
}

function toDisplayRows(rows: IPromptDiffRow[]): DisplayRow[] {
  return rows.flatMap((row): DisplayRow[] => {
    if (row.op === 'replace') {
      return [
        {
          op: row.op,
          oldLine: row.oldLine,
          newLine: null,
          text: row.oldText ?? '',
          marker: '−',
          tone: 'delete'
        },
        {
          op: row.op,
          oldLine: null,
          newLine: row.newLine,
          text: row.newText ?? '',
          marker: '+',
          tone: 'insert'
        }
      ];
    }
    if (row.op === 'insert') {
      return [
        {
          op: row.op,
          oldLine: null,
          newLine: row.newLine,
          text: row.newText ?? '',
          marker: '+',
          tone: 'insert'
        }
      ];
    }
    if (row.op === 'delete') {
      return [
        {
          op: row.op,
          oldLine: row.oldLine,
          newLine: null,
          text: row.oldText ?? '',
          marker: '−',
          tone: 'delete'
        }
      ];
    }
    return [
      {
        op: row.op,
        oldLine: row.oldLine,
        newLine: row.newLine,
        text: row.oldText ?? '',
        marker: ' ',
        tone: 'equal'
      }
    ];
  });
}

const TONE_ROW: Record<DisplayRow['tone'], string> = {
  equal: '',
  insert: 'bg-emerald-500/10',
  delete: 'bg-red-500/10'
};

const TONE_TEXT: Record<DisplayRow['tone'], string> = {
  equal: 'text-foreground',
  insert: 'text-emerald-700 dark:text-emerald-300',
  delete: 'text-red-700 dark:text-red-300'
};

/**
 * Presentation-only diff rows shared by the prompt and skill update dialogs.
 * `oldText`/`newText` are plain text and are never rendered as HTML.
 */
export function VersionDiffRows({
  rows,
  viewMode,
  testId = 'prompt-diff-row'
}: Props) {
  if (viewMode === 'unified') {
    return (
      <table className="w-full border-collapse text-xs font-mono">
        <tbody>
          {toDisplayRows(rows).map((row, index) => (
            <tr
              key={index}
              data-testid={testId}
              data-op={row.op}
              className={TONE_ROW[row.tone]}
            >
              <td className="w-8 select-none border-r px-1 text-right text-[10px] text-muted-foreground">
                {row.oldLine ?? ''}
              </td>
              <td className="w-8 select-none border-r px-1 text-right text-[10px] text-muted-foreground">
                {row.newLine ?? ''}
              </td>
              <td
                aria-hidden="true"
                className={`w-5 select-none border-r text-center font-semibold ${TONE_TEXT[row.tone]}`}
              >
                {row.marker}
              </td>
              <td
                className={`whitespace-pre-wrap px-2 py-1 ${TONE_TEXT[row.tone]}`}
              >
                {row.text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  return (
    <table className="w-full border-collapse text-xs font-mono">
      <tbody>
        {rows.map((row, index) => (
          <tr
            key={index}
            data-testid={testId}
            data-op={row.op}
            className={OP_CLASS[row.op]}
          >
            <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
              {row.oldLine ?? ''}
            </td>
            <td
              className="w-1/2 whitespace-pre-wrap border-r px-2 py-0.5"
              data-side="old"
            >
              {row.oldText ?? ''}
            </td>
            <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
              {row.newLine ?? ''}
            </td>
            <td
              className="w-1/2 whitespace-pre-wrap px-2 py-0.5"
              data-side="new"
            >
              {row.newText ?? ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default VersionDiffRows;
