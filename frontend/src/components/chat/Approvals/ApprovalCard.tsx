import {
  ChainlitContext,
  IApproval,
  IApprovalEditField,
  useChainlitStore,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import { MessageContext } from 'contexts/MessageContext';
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

// JSON leaves stay quoted so string/number/boolean/null stay distinguishable,
// but embedded newlines and tabs are shown as real layout inside the <pre>
// instead of two-character "\n" / "\t" escapes. Other escapes (including a
// literal backslash) are left untouched so the value stays unambiguous.
const formatLeaf = (value: unknown): string => {
  const json = JSON.stringify(value);
  if (json === undefined) return 'undefined';
  if (typeof value !== 'string') return json;
  let formatted = '';
  for (let index = 0; index < json.length; index += 1) {
    const char = json[index];
    if (char !== '\\' || index + 1 >= json.length) {
      formatted += char;
      continue;
    }
    const next = json[index + 1];
    if (next === 'n') {
      formatted += '\n';
      index += 1;
      continue;
    }
    if (next === 't') {
      formatted += '\t';
      index += 1;
      continue;
    }
    formatted += char + next;
    index += 1;
  }
  return formatted;
};

const flattenPreview = (
  value: unknown,
  prefix: string,
  leaves: Map<string, string>
): Map<string, string> => {
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

// Diff of redacted previews only: the full arguments never reach this view
// after a reload.
const diffPreviews = (before: Preview, after: Preview): string[] => {
  if (!after) return [];
  const left = flattenPreview(before ?? {}, '', new Map());
  const right = flattenPreview(after, '', new Map());
  const lines: string[] = [];
  new Set([...left.keys(), ...right.keys()]).forEach((path) => {
    const from = left.get(path);
    const to = right.get(path);
    if (from === to) return;
    if (from === undefined) lines.push(`+ ${path}: ${to}`);
    else if (to === undefined) lines.push(`- ${path}: ${from}`);
    else lines.push(`~ ${path}: ${from} → ${to}`);
  });
  return lines.sort();
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
          {field.required ? null : <option value="">—</option>}
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
          {approval.status === 'edited' ? (
            <p className="mt-1 text-xs text-muted-foreground">
              Requested content; the approved changes are in the diff.
            </p>
          ) : null}
        </div>
      ) : null}

      {diff.length ? (
        <div data-testid="approval-diff" className="mt-3">
          <div className="text-xs font-medium text-muted-foreground">
            Original → edited
          </div>
          <pre className="mt-1 max-h-[80vh] overflow-auto custom-scrollbar rounded bg-muted p-3 text-xs">
            {diff.join('\n')}
          </pre>
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
        <div className="mt-3 flex flex-wrap gap-2">
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
        <div className="mt-3 text-xs text-muted-foreground">
          {approval.decidedBy ? `By ${approval.decidedBy}` : null}
          {approval.decidedBy && approval.decidedAt ? ' · ' : null}
          {approval.decidedAt ?? approval.terminalReason ?? null}
          {approval.reason ? (
            <div
              data-testid="approval-comment"
              className="mt-1 whitespace-pre-wrap text-xs"
            >
              <bdi dir="auto">{approval.reason}</bdi>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
};
