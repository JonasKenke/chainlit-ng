import { useTranslation } from '@/components/i18n/Translator';

type Preview = Record<string, unknown> | null | undefined;

type LeafType = 'string' | 'number' | 'boolean' | 'null' | 'other';

interface DiffLeaf {
  text: string;
  type: LeafType;
}

type DiffOperation = 'added' | 'removed' | 'replaced';

interface DiffEntry {
  path: string;
  operation: DiffOperation;
  from: DiffLeaf | null;
  to: DiffLeaf | null;
}

interface DiffGroup {
  key: string;
  label: string;
  entries: DiffEntry[];
}

// Strings lose their JSON quotes so rows read as prose, but keep embedded
// newlines and tabs as real layout; other escapes stay literal so a value
// remains unambiguous. Non-string leaves keep their JSON literal and are
// rendered monospace, so the string "true" stays distinct from true.
const formatLeaf = (value: unknown): DiffLeaf => {
  if (typeof value === 'string') {
    const json = JSON.stringify(value).slice(1, -1);
    let text = '';
    for (let index = 0; index < json.length; index += 1) {
      const char = json[index];
      if (char !== '\\' || index + 1 >= json.length) {
        text += char;
        continue;
      }
      const next = json[index + 1];
      if (next === 'n') {
        text += '\n';
        index += 1;
        continue;
      }
      if (next === 't') {
        text += '\t';
        index += 1;
        continue;
      }
      text += char + next;
      index += 1;
    }
    return { text, type: 'string' };
  }
  if (value === null) return { text: 'null', type: 'null' };
  if (typeof value === 'number') {
    return { text: String(value), type: 'number' };
  }
  if (typeof value === 'boolean') {
    return { text: String(value), type: 'boolean' };
  }
  const json = JSON.stringify(value);
  return { text: json ?? 'undefined', type: 'other' };
};

const flattenPreview = (
  value: unknown,
  prefix: string,
  leaves: Map<string, DiffLeaf>
): Map<string, DiffLeaf> => {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      flattenPreview(item, `${prefix}[${index}]`, leaves)
    );
    return leaves;
  }
  if (value && typeof value === 'object') {
    Object.entries(value as Record<string, unknown>).forEach(([key, item]) =>
      flattenPreview(item, prefix ? `${prefix}.${key}` : key, leaves)
    );
    return leaves;
  }
  leaves.set(prefix, formatLeaf(value));
  return leaves;
};

// Agent-facing paths stay compact (`labels[2]`, `nested.api_key`); the view
// splits them again so each segment can be humanized for display.
const humanizeSegment = (segment: string): string => {
  if (/^\d+$/.test(segment)) return `item ${Number(segment) + 1}`;
  const words = segment
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase();
  return words || segment;
};

const humanizePath = (path: string, skipTop = false): string => {
  const parts = path.match(/[^.[\]]+/g) ?? [];
  const segments = (skipTop ? parts.slice(1) : parts).map(humanizeSegment);
  if (!segments.length) return skipTop ? '' : 'Value';
  if (!skipTop) {
    segments[0] = segments[0].charAt(0).toUpperCase() + segments[0].slice(1);
  }
  return segments.join(' · ');
};

// Diff of redacted previews only: the full arguments never reach this view
// after a reload.
const diffPreviews = (before: Preview, after: Preview): DiffEntry[] => {
  if (!after) return [];
  const left = flattenPreview(before ?? {}, '', new Map());
  const right = flattenPreview(after, '', new Map());
  const entries: DiffEntry[] = [];
  new Set([...left.keys(), ...right.keys()]).forEach((path) => {
    const from = left.get(path);
    const to = right.get(path);
    if (from && to) {
      if (from.text === to.text && from.type === to.type) return;
      entries.push({ path, operation: 'replaced', from, to });
      return;
    }
    if (from) entries.push({ path, operation: 'removed', from, to: null });
    else if (to) entries.push({ path, operation: 'added', from: null, to });
  });
  return entries.sort((a, b) => a.path.localeCompare(b.path));
};

const groupDiff = (entries: DiffEntry[]): DiffGroup[] => {
  const groups = new Map<string, DiffGroup>();
  for (const entry of entries) {
    const key = entry.path.match(/[^.[\]]+/)?.[0] ?? entry.path;
    const group = groups.get(key) ?? {
      key,
      label: humanizePath(key),
      entries: []
    };
    group.entries.push(entry);
    groups.set(key, group);
  }
  return [...groups.values()];
};

const operationMeta: Record<
  DiffOperation,
  { marker: string; labelKey: string; label: string; tone: string }
> = {
  added: {
    marker: '+',
    labelKey: 'chat.approval.added',
    label: 'Added',
    tone: 'text-emerald-600 dark:text-emerald-400'
  },
  removed: {
    marker: '-',
    labelKey: 'chat.approval.removed',
    label: 'Removed',
    tone: 'text-red-600 dark:text-red-400'
  },
  replaced: {
    marker: '~',
    labelKey: 'chat.approval.replaced',
    label: 'Replaced',
    tone: 'text-amber-600 dark:text-amber-400'
  }
};

// Values are plain React text, never HTML or markdown. Non-string leaves keep
// a monospace face so numbers, booleans and null stay distinguishable from
// the string with the same characters.
const DiffValue = ({ leaf, tone }: { leaf: DiffLeaf; tone?: string }) => (
  <span
    data-value-type={leaf.type}
    className={`whitespace-pre-wrap break-words ${
      leaf.type === 'string' ? '' : 'font-mono'
    } ${tone ?? ''}`}
  >
    {leaf.text}
  </span>
);

const DiffRow = ({
  entry,
  grouped
}: {
  entry: DiffEntry;
  grouped: boolean;
}) => {
  const { t } = useTranslation();
  const meta = operationMeta[entry.operation];
  const operationLabel = t(meta.labelKey, { defaultValue: meta.label });
  const label =
    (grouped ? humanizePath(entry.path, true) : '') || humanizePath(entry.path);
  // A replacement whose either side spans lines reads better as an old block
  // followed by a new block, so real newlines are not flattened into a row.
  const multiline =
    entry.operation === 'replaced' &&
    Boolean(
      entry.from &&
      entry.to &&
      (entry.from.text.includes('\n') || entry.to.text.includes('\n'))
    );
  return (
    <li
      data-testid="approval-diff-row"
      data-operation={entry.operation}
      className="flex gap-2"
    >
      <span
        aria-hidden="true"
        className={`w-3 shrink-0 font-mono font-semibold ${meta.tone}`}
      >
        {meta.marker}
      </span>
      <span className="sr-only">{`${operationLabel}: `}</span>
      <div className="min-w-0 flex-1">
        {multiline ? (
          <>
            <div className="font-medium text-foreground">{label}</div>
            <div className="mt-1 space-y-1 border-l border-border pl-2">
              {entry.from ? (
                <div className="flex gap-2">
                  <span
                    aria-hidden="true"
                    className="w-3 shrink-0 font-mono font-semibold text-red-600 dark:text-red-400"
                  >
                    -
                  </span>
                  <span className="sr-only">
                    {t('chat.approval.before', {
                      defaultValue: 'Before:'
                    })}{' '}
                  </span>
                  <DiffValue leaf={entry.from} tone="text-muted-foreground" />
                </div>
              ) : null}
              {entry.to ? (
                <div className="flex gap-2">
                  <span
                    aria-hidden="true"
                    className="w-3 shrink-0 font-mono font-semibold text-emerald-600 dark:text-emerald-400"
                  >
                    +
                  </span>
                  <span className="sr-only">
                    {t('chat.approval.after', { defaultValue: 'After:' })}{' '}
                  </span>
                  <DiffValue leaf={entry.to} />
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <div className="break-words">
            <span className="font-medium text-foreground">{label}</span>
            <span className="text-muted-foreground">{': '}</span>
            {entry.from ? (
              <DiffValue
                leaf={entry.from}
                tone={
                  entry.operation === 'removed' ? meta.tone : 'text-foreground'
                }
              />
            ) : null}
            {entry.operation === 'replaced' && entry.from && entry.to ? (
              <span aria-hidden="true" className="text-muted-foreground">
                {' → '}
              </span>
            ) : null}
            {entry.to ? (
              <DiffValue
                leaf={entry.to}
                tone={
                  entry.operation === 'added' ? meta.tone : 'text-foreground'
                }
              />
            ) : null}
          </div>
        )}
      </div>
    </li>
  );
};

export const ApprovalDiff = ({ entries }: { entries: DiffEntry[] }) => {
  const { t } = useTranslation();
  if (!entries.length) return null;
  const groups = groupDiff(entries);

  return (
    <div data-testid="approval-diff" className="mt-3">
      <div className="text-xs font-medium text-muted-foreground">
        {t('chat.approval.originalToEdited', {
          defaultValue: 'Original → edited'
        })}
      </div>
      <div className="mt-1 max-h-[80vh] space-y-2 overflow-auto custom-scrollbar rounded bg-muted p-3 text-xs">
        {groups.map((group) => {
          const grouped = group.entries.length > 1;
          return (
            <div key={group.key}>
              {grouped ? (
                <div className="font-semibold text-foreground">
                  {group.label}
                </div>
              ) : null}
              <ul className={grouped ? 'mt-1 space-y-1' : 'space-y-1'}>
                {group.entries.map((entry) => (
                  <DiffRow key={entry.path} entry={entry} grouped={grouped} />
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export { diffPreviews };
export type { DiffEntry };
