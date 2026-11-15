import type { ISkillDiff, ISkillDiffRow } from '@chainlit-ng/react-client';

/** Matches the server-side prompt diff bounds so both dialogs feel the same. */
export const MAX_SKILL_DIFF_ROWS = 2000;
export const MAX_SKILL_DIFF_BYTES = 256 * 1024;
/** Above this many DP cells the browser falls back to a coarse row block. */
const MAX_DIFF_CELLS = 250_000;

interface RawRow {
  op: 'equal' | 'delete' | 'insert';
  oldLine: number | null;
  newLine: number | null;
  oldText: string | null;
  newText: string | null;
}

function splitLines(value: string): string[] {
  if (value === '') return [];
  return value.split('\n');
}

function lcsRows(oldLines: string[], newLines: string[]): RawRow[] | null {
  const n = oldLines.length;
  const m = newLines.length;
  if ((n + 1) * (m + 1) > MAX_DIFF_CELLS) return null;

  const table = new Uint32Array((n + 1) * (m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * (m + 1) + j] =
        oldLines[i] === newLines[j]
          ? table[(i + 1) * (m + 1) + j + 1] + 1
          : Math.max(table[(i + 1) * (m + 1) + j], table[i * (m + 1) + j + 1]);
    }
  }

  const rows: RawRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (oldLines[i] === newLines[j]) {
      rows.push({
        op: 'equal',
        oldLine: i + 1,
        newLine: j + 1,
        oldText: oldLines[i],
        newText: newLines[j]
      });
      i += 1;
      j += 1;
      continue;
    }
    if (table[(i + 1) * (m + 1) + j] >= table[i * (m + 1) + j + 1]) {
      rows.push({
        op: 'delete',
        oldLine: i + 1,
        newLine: null,
        oldText: oldLines[i],
        newText: null
      });
      i += 1;
    } else {
      rows.push({
        op: 'insert',
        oldLine: null,
        newLine: j + 1,
        oldText: null,
        newText: newLines[j]
      });
      j += 1;
    }
  }
  while (i < n) {
    rows.push({
      op: 'delete',
      oldLine: i + 1,
      newLine: null,
      oldText: oldLines[i],
      newText: null
    });
    i += 1;
  }
  while (j < m) {
    rows.push({
      op: 'insert',
      oldLine: null,
      newLine: j + 1,
      oldText: null,
      newText: newLines[j]
    });
    j += 1;
  }
  return rows;
}

function coarseRows(oldLines: string[], newLines: string[]): RawRow[] {
  const rows: RawRow[] = [];
  oldLines.forEach((line, index) => {
    rows.push({
      op: 'delete',
      oldLine: index + 1,
      newLine: null,
      oldText: line,
      newText: null
    });
  });
  newLines.forEach((line, index) => {
    rows.push({
      op: 'insert',
      oldLine: null,
      newLine: index + 1,
      oldText: null,
      newText: line
    });
  });
  return rows;
}

/** Pair a delete run followed by an insert run into side-by-side rows. */
function pairChanges(rows: RawRow[]): ISkillDiffRow[] {
  const result: ISkillDiffRow[] = [];
  let index = 0;
  while (index < rows.length) {
    const row = rows[index];
    if (row.op === 'equal') {
      result.push(row);
      index += 1;
      continue;
    }

    const deleted: RawRow[] = [];
    while (index < rows.length && rows[index].op === 'delete') {
      deleted.push(rows[index]);
      index += 1;
    }
    const inserted: RawRow[] = [];
    while (index < rows.length && rows[index].op === 'insert') {
      inserted.push(rows[index]);
      index += 1;
    }

    // A lone insert (no preceding delete) stays an insert.
    if (deleted.length === 0) {
      result.push(...inserted);
      continue;
    }

    const span = Math.max(deleted.length, inserted.length);
    for (let offset = 0; offset < span; offset += 1) {
      const oldRow = deleted[offset];
      const newRow = inserted[offset];
      if (oldRow && newRow) {
        result.push({
          op: 'replace',
          oldLine: oldRow.oldLine,
          newLine: newRow.newLine,
          oldText: oldRow.oldText,
          newText: newRow.newText
        });
      } else if (oldRow) {
        result.push(oldRow);
      } else if (newRow) {
        result.push(newRow);
      }
    }
  }
  return result;
}

/**
 * Build bounded, presentation-only diff rows for two text bodies. The backend
 * has no skill diff route, so the skill and prompt dialogs derive them here;
 * text is never HTML and the output never reaches the model.
 */
export function computeTextDiff(
  oldContent: string,
  newContent: string
): ISkillDiff {
  const oldLines = splitLines(oldContent);
  const newLines = splitLines(newContent);
  const raw = lcsRows(oldLines, newLines);
  const rows = pairChanges(raw ?? coarseRows(oldLines, newLines));

  let usedBytes = 0;
  let truncated = raw === null;
  const bounded: ISkillDiffRow[] = [];
  for (const row of rows) {
    if (bounded.length >= MAX_SKILL_DIFF_ROWS) {
      truncated = true;
      break;
    }
    const size =
      new TextEncoder().encode(row.oldText ?? '').length +
      new TextEncoder().encode(row.newText ?? '').length;
    if (usedBytes + size > MAX_SKILL_DIFF_BYTES) {
      truncated = true;
      break;
    }
    usedBytes += size;
    bounded.push(row);
  }

  return { rows: bounded, truncated };
}
