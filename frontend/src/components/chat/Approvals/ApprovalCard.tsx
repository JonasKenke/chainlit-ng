import {
  ChainlitContext,
  IApproval,
  IApprovalEditField,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import { MessageContext } from 'contexts/MessageContext';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  MessageSquare,
  MinusCircle,
  XCircle
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import { toast } from 'sonner';
import { v4 as uuidv4 } from 'uuid';

import { Markdown } from '@/components/Markdown';
import { Button } from '@/components/ui/button';

// Editable controls live in scrollable card bodies, where an outward focus
// ring is clipped at the left edge. Draw the ring inside the control instead.
const focusRing =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary';

const displayToolName = (tool: string): string =>
  tool
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());

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
  { marker: string; label: string; tone: string }
> = {
  added: {
    marker: '+',
    label: 'Added',
    tone: 'text-emerald-600 dark:text-emerald-400'
  },
  removed: {
    marker: '-',
    label: 'Removed',
    tone: 'text-red-600 dark:text-red-400'
  },
  replaced: {
    marker: '~',
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
  const meta = operationMeta[entry.operation];
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
      <span className="sr-only">{`${meta.label}: `}</span>
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
                  <span className="sr-only">Before: </span>
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
                  <span className="sr-only">After: </span>
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

const approvalDecisionLabel = (status: IApproval['status']): string | null => {
  switch (status) {
    case 'approved':
      return 'Approved';
    case 'edited':
      return 'Approved with edits';
    case 'rejected':
      return 'Rejected';
    case 'changes_requested':
      return 'Changes requested';
    case 'expired':
      return 'Expired';
    case 'superseded':
      return 'Superseded';
    case 'orphaned':
      return 'Unavailable';
    default:
      return null;
  }
};

const approvalDecisionMeta = (
  status: IApproval['status']
): { icon: LucideIcon; tone: string } => {
  switch (status) {
    case 'approved':
    case 'edited':
      return {
        icon: CheckCircle2,
        tone: 'text-emerald-600 dark:text-emerald-400'
      };
    case 'rejected':
      return { icon: XCircle, tone: 'text-red-600 dark:text-red-400' };
    case 'changes_requested':
      return {
        icon: MessageSquare,
        tone: 'text-amber-600 dark:text-amber-400'
      };
    case 'expired':
      return { icon: Clock, tone: 'text-muted-foreground' };
    case 'superseded':
      return { icon: MinusCircle, tone: 'text-muted-foreground' };
    default:
      return { icon: AlertTriangle, tone: 'text-muted-foreground' };
  }
};

const approvalErrorMessage = (error: unknown): string => {
  if (typeof error === 'object' && error !== null && 'payload' in error) {
    const payload = error.payload;
    if (
      typeof payload === 'object' &&
      payload !== null &&
      'detail' in payload
    ) {
      const detail = payload.detail;
      if (typeof detail === 'string' && detail) return detail;
      if (typeof detail === 'object' && detail !== null) {
        // 422 ApprovalInvalidArguments carries the schema field path and the
        // Pydantic error type; never the submitted value.
        const field = 'field' in detail ? detail.field : undefined;
        const type = 'type' in detail ? detail.type : undefined;
        if (typeof field === 'string') {
          return typeof type === 'string' ? `${field} (${type})` : field;
        }
      }
    }
  }
  return error instanceof Error ? error.message : 'Unable to decide approval';
};

type FieldValues = Record<string, string | boolean>;

const isServerHeld = (field: IApprovalEditField): boolean =>
  field.serverHeld === true || field.secret === true;

const initialFieldValues = (
  fields: IApprovalEditField[],
  args: Record<string, unknown>
): FieldValues => {
  const values: FieldValues = {};
  for (const field of fields) {
    if (isServerHeld(field)) {
      values[field.name] = '';
      continue;
    }
    const value = args[field.name] ?? field.default;
    if (field.kind === 'boolean') {
      values[field.name] = Boolean(value);
    } else if (field.kind === 'string_list') {
      values[field.name] = Array.isArray(value) ? value.join('\n') : '';
    } else {
      values[field.name] =
        value === null || value === undefined ? '' : String(value);
    }
  }
  return values;
};

const buildEditedArguments = (
  fields: IApprovalEditField[],
  values: FieldValues
): Record<string, unknown> | string => {
  const edited: Record<string, unknown> = {};
  for (const field of fields) {
    if (isServerHeld(field)) continue;
    const raw = values[field.name];
    if (field.kind === 'boolean') {
      edited[field.name] = Boolean(raw);
      continue;
    }
    const text = typeof raw === 'string' ? raw : '';
    if (field.kind === 'string_list') {
      edited[field.name] = text
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean);
      continue;
    }
    if (text.trim() === '') {
      if (field.required) return `${field.label} is required`;
      if (field.nullable) edited[field.name] = null;
      continue;
    }
    if (field.kind === 'enum') {
      const match = (field.options ?? []).find(
        (option) => String(option) === text
      );
      edited[field.name] = match ?? text;
      continue;
    }
    if (field.kind === 'integer') {
      const number = Number(text);
      if (!Number.isInteger(number)) {
        return `${field.label} must be a whole number`;
      }
      edited[field.name] = number;
      continue;
    }
    if (field.kind === 'number') {
      const number = Number(text);
      if (!Number.isFinite(number)) {
        return `${field.label} must be a number`;
      }
      edited[field.name] = number;
      continue;
    }
    edited[field.name] = text;
  }
  return edited;
};

const argumentsEqual = (left: unknown, right: unknown): boolean => {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    return (
      left.length === right.length &&
      left.every((item, index) => argumentsEqual(item, right[index]))
    );
  }
  if (
    left === null ||
    right === null ||
    typeof left !== 'object' ||
    typeof right !== 'object'
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  return (
    leftKeys.length === Object.keys(rightRecord).length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(rightRecord, key) &&
        argumentsEqual(leftRecord[key], rightRecord[key])
    )
  );
};

const withServerHeldArguments = (
  values: Record<string, unknown>,
  fields: IApprovalEditField[],
  args: Record<string, unknown>
): Record<string, unknown> => {
  const merged = { ...values };
  for (const field of fields) {
    if (isServerHeld(field) && field.name in args) {
      merged[field.name] = args[field.name];
    }
  }
  return merged;
};

// An untouched form must not become an edit: run the live arguments through
// the same normalisation as the form and compare, merging server-held values
// back in because the form never renders or submits them.
const areArgumentsUnchanged = (
  fields: IApprovalEditField[],
  args: Record<string, unknown>,
  edited: Record<string, unknown>
): boolean => {
  const baseline = buildEditedArguments(
    fields,
    initialFieldValues(fields, args)
  );
  if (typeof baseline === 'string') return false;
  return argumentsEqual(
    withServerHeldArguments(edited, fields, args),
    withServerHeldArguments(baseline, fields, args)
  );
};

interface Props {
  approval: IApproval;
}

export const ApprovalCard = ({ approval }: Props) => {
  const apiClient = useContext(ChainlitContext);
  const { allowHtml, latex } = useContext(MessageContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const setApprovals = useSetChainlitState('approvals');
  const [busy, setBusy] = useState(false);
  const [editMode, setEditMode] = useState<'form' | 'json' | null>(null);
  const [draft, setDraft] = useState('');
  const [fieldValues, setFieldValues] = useState<FieldValues>({});
  const [requestingChanges, setRequestingChanges] = useState(false);
  const [comment, setComment] = useState('');
  const editAreaRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const editTriggerRef = useRef<HTMLButtonElement>(null);
  const requestChangesTriggerRef = useRef<HTMLButtonElement>(null);
  const firstFieldRef = useRef<
    HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
  >(null);
  const commentRef = useRef<HTMLTextAreaElement>(null);
  const jsonEditorRef = useRef<HTMLTextAreaElement>(null);
  // Where focus should land once the next render unmounts the focused control.
  const pendingFocusRef = useRef<'card' | 'edit' | 'request' | null>(null);

  useEffect(() => {
    if (!editMode) return;
    editAreaRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start'
    });
    if (editMode === 'form') {
      firstFieldRef.current?.focus();
    } else {
      jsonEditorRef.current?.focus();
    }
  }, [editMode]);

  useEffect(() => {
    if (requestingChanges) {
      commentRef.current?.focus();
    }
  }, [requestingChanges]);

  useEffect(() => {
    const target = pendingFocusRef.current;
    if (!target) return;
    if (target === 'edit' && editMode) return;
    if (target === 'request' && requestingChanges) return;
    pendingFocusRef.current = null;
    if (target === 'card') {
      cardRef.current?.focus();
    } else if (target === 'edit') {
      (editTriggerRef.current ?? cardRef.current)?.focus();
    } else {
      (requestChangesTriggerRef.current ?? cardRef.current)?.focus();
    }
  }, [editMode, requestingChanges]);

  const fields = Array.isArray(approval.editFields)
    ? approval.editFields
    : null;
  const canEdit = Boolean(approval.editAllowed) && approval.live !== false;
  // Absent means allowed: older servers never send the flag.
  const canRequestChanges = approval.requestChangesAllowed !== false;
  const isPending = approval.status === 'pending';
  const diff = useMemo(
    () =>
      diffPreviews(approval.argumentsPreview, approval.editedArgumentsPreview),
    [approval.argumentsPreview, approval.editedArgumentsPreview]
  );
  const decisionLabel = approvalDecisionLabel(approval.status);
  const decisionMeta = approvalDecisionMeta(approval.status);
  const DecisionIcon = decisionMeta.icon;
  const diffGroups = useMemo(() => groupDiff(diff), [diff]);
  const firstEditableFieldName =
    fields?.find((field) => !isServerHeld(field))?.name ?? null;
  const setFirstFieldElement = useCallback(
    (
      element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null
    ) => {
      firstFieldRef.current = element;
    },
    []
  );

  const decide = async (
    decision: 'approve' | 'reject' | 'edit' | 'request_changes',
    options: { editedArguments?: Record<string, unknown>; reason?: string } = {}
  ) => {
    setBusy(true);
    try {
      const response = await apiClient.respondToApproval({
        sessionId,
        approvalId: approval.id,
        decision,
        ...(options.editedArguments
          ? { editedArguments: options.editedArguments }
          : {}),
        ...(options.reason ? { reason: options.reason } : {}),
        decisionRequestId: uuidv4()
      });
      setApprovals((old) =>
        old.map((item) =>
          item.id === response.approvalId
            ? {
                ...item,
                status: response.status,
                decision: response.decision,
                decidedBy: response.decidedBy,
                decidedAt: response.decidedAt,
                terminalReason: response.terminalReason,
                reason: response.reason ?? options.reason ?? item.reason,
                revision: response.revision,
                ...(options.editedArguments
                  ? { arguments: options.editedArguments }
                  : {})
              }
            : item
        )
      );
    } catch (error) {
      toast.error(approvalErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = () => {
    if (fields && fields.length) {
      setFieldValues(initialFieldValues(fields, approval.arguments ?? {}));
      setEditMode('form');
      return;
    }
    setDraft(JSON.stringify(approval.arguments ?? {}, null, 2));
    setEditMode('json');
  };

  const submitForm = () => {
    if (!fields) return;
    const edited = buildEditedArguments(fields, fieldValues);
    if (typeof edited === 'string') {
      toast.error(edited);
      return;
    }
    if (areArgumentsUnchanged(fields, approval.arguments ?? {}, edited)) {
      pendingFocusRef.current = 'card';
      setEditMode(null);
      toast.message('No changes to save');
      return;
    }
    pendingFocusRef.current = 'card';
    setEditMode(null);
    void decide('edit', { editedArguments: edited });
  };

  const submitJson = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch {
      toast.error('Arguments must be valid JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      toast.error('Arguments must be a JSON object');
      return;
    }
    pendingFocusRef.current = 'card';
    setEditMode(null);
    void decide('edit', { editedArguments: parsed as Record<string, unknown> });
  };

  const cancelEdit = () => {
    pendingFocusRef.current = 'edit';
    setEditMode(null);
  };

  const startRequestChanges = () => {
    setComment('');
    setRequestingChanges(true);
  };

  const cancelRequestChanges = () => {
    pendingFocusRef.current = 'request';
    setRequestingChanges(false);
  };

  const submitRequestChanges = () => {
    const reason = comment.trim();
    if (!reason) {
      toast.error('A comment is required when requesting changes');
      return;
    }
    pendingFocusRef.current = 'card';
    setRequestingChanges(false);
    void decide('request_changes', { reason });
  };

  const renderField = (field: IApprovalEditField) => {
    const id = `approval-${approval.id}-${field.name}`;
    const value = fieldValues[field.name];
    const className = `mt-1 w-full rounded border border-border bg-background p-2 text-sm ${focusRing}`;
    const ref =
      field.name === firstEditableFieldName ? setFirstFieldElement : undefined;
    if (isServerHeld(field)) {
      return (
        <input
          id={id}
          aria-label={field.label}
          type="text"
          className={`${className} text-muted-foreground`}
          value="Server-held"
          readOnly
          disabled
        />
      );
    }
    if (field.kind === 'boolean') {
      return (
        <input
          ref={ref}
          id={id}
          aria-label={field.label}
          type="checkbox"
          className={`mt-1 h-4 w-4 rounded border-border ${focusRing}`}
          checked={Boolean(value)}
          onChange={(event) =>
            setFieldValues((old) => ({
              ...old,
              [field.name]: event.target.checked
            }))
          }
        />
      );
    }
    if (field.kind === 'enum') {
      return (
        <select
          ref={ref}
          id={id}
          aria-label={field.label}
          className={className}
          value={typeof value === 'string' ? value : ''}
          onChange={(event) =>
            setFieldValues((old) => ({
              ...old,
              [field.name]: event.target.value
            }))
          }
        >
          {field.required ? null : <option value="">None</option>}
          {(field.options ?? []).map((option) => (
            <option key={String(option)} value={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
      );
    }
    if (field.kind === 'text' || field.kind === 'string_list') {
      return (
        <textarea
          ref={ref}
          id={id}
          aria-label={field.label}
          className={`${className} h-28`}
          placeholder={
            field.kind === 'string_list'
              ? 'One per line or comma separated'
              : undefined
          }
          value={typeof value === 'string' ? value : ''}
          onChange={(event) =>
            setFieldValues((old) => ({
              ...old,
              [field.name]: event.target.value
            }))
          }
        />
      );
    }
    return (
      <input
        ref={ref}
        id={id}
        aria-label={field.label}
        type={
          field.kind === 'integer' || field.kind === 'number'
            ? 'number'
            : 'text'
        }
        step={
          field.kind === 'integer'
            ? 1
            : field.kind === 'number'
              ? 'any'
              : undefined
        }
        className={className}
        value={typeof value === 'string' ? value : ''}
        onChange={(event) =>
          setFieldValues((old) => ({
            ...old,
            [field.name]: event.target.value
          }))
        }
      />
    );
  };

  return (
    <section
      ref={cardRef}
      tabIndex={-1}
      data-testid="approval-card"
      data-approval-id={approval.id}
      aria-label={`Approval for ${approval.tool}`}
      className="scroll-mt-4 rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div>
        <div data-testid="approval-tool" className="text-sm font-semibold">
          {displayToolName(approval.tool)}
        </div>
        {approval.title ? (
          <div className="text-sm text-muted-foreground">{approval.title}</div>
        ) : null}
      </div>

      {approval.content ? (
        <div
          data-testid="approval-content"
          className="mt-3 max-h-[80vh] overflow-y-auto custom-scrollbar pr-1 text-sm [&_td]:py-2 [&_th]:py-2 [&_th]:h-10"
        >
          <Markdown allowHtml={allowHtml} latex={latex}>
            {approval.content}
          </Markdown>
          {approval.status === 'edited' && diff.length ? (
            <p className="mt-1 text-xs text-muted-foreground">
              Edited content; the approved changes are in the diff.
            </p>
          ) : null}
        </div>
      ) : null}

      {diff.length ? (
        <div data-testid="approval-diff" className="mt-3">
          <div className="text-xs font-medium text-muted-foreground">
            Original → edited
          </div>
          <div className="mt-1 max-h-[80vh] space-y-2 overflow-auto custom-scrollbar rounded bg-muted p-3 text-xs">
            {diffGroups.map((group) => {
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
                      <DiffRow
                        key={entry.path}
                        entry={entry}
                        grouped={grouped}
                      />
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {editMode === 'form' && fields ? (
        <div
          ref={editAreaRef}
          data-testid="approval-edit-form"
          className="mt-4 scroll-mt-4 border-t border-border pt-3"
        >
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Edit fields
          </div>
          <div className="mt-3 max-h-[80vh] space-y-3 overflow-y-auto custom-scrollbar pr-1">
            {fields.map((field) => (
              <div key={field.name}>
                <label
                  htmlFor={`approval-${approval.id}-${field.name}`}
                  className="text-xs font-medium text-muted-foreground"
                >
                  {field.label}
                  {field.required ? ' *' : ''}
                </label>
                {renderField(field)}
                {field.description ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {field.description}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-2 pb-2">
            <Button
              type="button"
              size="sm"
              disabled={busy}
              onClick={submitForm}
            >
              Save & approve
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelEdit}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : editMode === 'json' ? (
        <div ref={editAreaRef} className="mt-3 scroll-mt-4">
          <textarea
            ref={jsonEditorRef}
            aria-label="Edited arguments JSON"
            className={`h-40 w-full rounded border border-border bg-background p-2 font-mono text-xs ${focusRing}`}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={busy}
              onClick={submitJson}
            >
              Save edit
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelEdit}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : requestingChanges ? (
        <div className="mt-3">
          <label
            htmlFor={`approval-comment-${approval.id}`}
            className="text-xs font-medium text-muted-foreground"
          >
            What should change? (required)
          </label>
          <textarea
            ref={commentRef}
            id={`approval-comment-${approval.id}`}
            aria-label="Comment"
            maxLength={500}
            className={`mt-1 h-24 w-full rounded border border-border bg-background p-2 text-sm ${focusRing}`}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <Button
              type="button"
              size="sm"
              disabled={busy || !comment.trim()}
              onClick={submitRequestChanges}
            >
              Request changes
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelRequestChanges}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : isPending ? (
        <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3">
          <Button
            type="button"
            size="sm"
            disabled={busy}
            onClick={() => decide('approve')}
          >
            Approve
          </Button>
          {canEdit ? (
            <Button
              ref={editTriggerRef}
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={startEdit}
            >
              Edit
            </Button>
          ) : null}
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => decide('reject')}
          >
            Reject
          </Button>
          {canRequestChanges ? (
            <Button
              ref={requestChangesTriggerRef}
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={startRequestChanges}
            >
              Request changes
            </Button>
          ) : null}
        </div>
      ) : (
        <>
          <div
            data-testid="approval-outcome"
            className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-border pt-3 text-xs"
          >
            <span className="inline-flex items-center gap-1.5">
              <DecisionIcon
                className={`size-3.5 shrink-0 ${decisionMeta.tone}`}
                aria-hidden="true"
              />
              <span
                data-testid="approval-decision"
                className="font-medium text-foreground"
              >
                {decisionLabel}
              </span>
            </span>
            {approval.terminalReason ? (
              <span className="text-muted-foreground">
                {approval.terminalReason.replace(/_/g, ' ')}
              </span>
            ) : null}
          </div>
          {approval.reason ? (
            <div
              data-testid="approval-comment"
              className="mt-2 whitespace-pre-wrap text-xs text-foreground"
            >
              <bdi dir="auto">{approval.reason}</bdi>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
};
