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
              <td className="w-10 select-none border-r px-2 text-right text-muted-foreground">
                {row.newLine ?? ''}
              </td>
              <td className="whitespace-pre-wrap px-2 py-0.5">
                {row.op === 'equal' ? (
                  <span>{row.oldText ?? ''}</span>
                ) : row.op === 'replace' ? (
                  <span>
                    <span className="block text-destructive line-through decoration-destructive/60">
                      {row.oldText ?? ''}
                    </span>
                    <span className="block text-emerald-600 dark:text-emerald-400">
                      {row.newText ?? ''}
                    </span>
                  </span>
                ) : (
                  <span
                    className={
                      row.op === 'insert'
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-destructive'
                    }
                  >
                    {row.op === 'insert'
                      ? (row.newText ?? '')
                      : (row.oldText ?? '')}
                  </span>
                )}
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
