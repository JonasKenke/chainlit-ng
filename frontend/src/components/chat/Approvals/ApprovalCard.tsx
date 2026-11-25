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
import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';

import { ApprovalDiff, diffPreviews } from './approval-diff';
import {
  EditValidationErrorKind,
  FieldValues,
  areArgumentsUnchanged,
  buildEditedArguments,
  initialFieldValues,
  isServerHeld
} from './approval-edit-utils';
import {
  approvalDecisionFallback,
  approvalDecisionKey,
  approvalDecisionMeta,
  approvalErrorMessage
} from './approval-utils';

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

interface Props {
  approval: IApproval;
  grouped?: boolean;
}

export const ApprovalCard = ({ approval, grouped = false }: Props) => {
  const apiClient = useContext(ChainlitContext);
  const { t } = useTranslation();
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
  // `title` is the deprecated server field for the same secondary text.
  const subtitle = approval.subtitle ?? approval.title;
  const diff = useMemo(
    () =>
      diffPreviews(approval.argumentsPreview, approval.editedArgumentsPreview),
    [approval.argumentsPreview, approval.editedArgumentsPreview]
  );
  const decisionKey = approvalDecisionKey(approval.status);
  const decisionLabel = decisionKey
    ? t(decisionKey, {
        defaultValue: approvalDecisionFallback(approval.status)
      })
    : null;
  const decisionMeta = approvalDecisionMeta(approval.status);
  const DecisionIcon = decisionMeta.icon;
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
      toast.error(
        approvalErrorMessage(
          error,
          t('chat.approval.errors.decide', {
            defaultValue: 'Unable to decide approval'
          })
        )
      );
    } finally {
      setBusy(false);
    }
  };

  const editErrorMessage = (
    kind: EditValidationErrorKind,
    field: string
  ): string => {
    if (kind === 'required') {
      return t('chat.approval.fieldErrors.required', {
        defaultValue: '{{field}} is required',
        field
      });
    }
    if (kind === 'wholeNumber') {
      return t('chat.approval.fieldErrors.wholeNumber', {
        defaultValue: '{{field}} must be a whole number',
        field
      });
    }
    return t('chat.approval.fieldErrors.number', {
      defaultValue: '{{field}} must be a number',
      field
    });
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
    if (!edited.ok) {
      toast.error(editErrorMessage(edited.kind, edited.field));
      return;
    }
    if (
      areArgumentsUnchanged(fields, approval.arguments ?? {}, edited.values)
    ) {
      pendingFocusRef.current = 'card';
      setEditMode(null);
      toast.message(
        t('chat.approval.noChanges', { defaultValue: 'No changes to save' })
      );
      return;
    }
    pendingFocusRef.current = 'card';
    setEditMode(null);
    void decide('edit', { editedArguments: edited.values });
  };

  const submitJson = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch {
      toast.error(
        t('chat.approval.invalidJson', {
          defaultValue: 'Arguments must be valid JSON'
        })
      );
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      toast.error(
        t('chat.approval.jsonObjectRequired', {
          defaultValue: 'Arguments must be a JSON object'
        })
      );
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
      toast.error(
        t('chat.approval.commentRequiredError', {
          defaultValue: 'A comment is required when requesting changes'
        })
      );
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
          value={t('chat.approval.serverHeld', {
            defaultValue: 'Server-held'
          })}
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
          {field.required ? null : (
            <option value="">
              {t('chat.approval.none', { defaultValue: 'None' })}
            </option>
          )}
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
              ? t('chat.approval.onePerLine', {
                  defaultValue: 'One per line or comma separated'
                })
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
      aria-label={t('chat.approval.ariaLabel', {
        defaultValue: 'Approval for {{tool}}',
        tool: approval.tool
      })}
      className={cn(
        'scroll-mt-4 rounded-lg border border-border bg-card p-4 text-card-foreground',
        grouped && 'border-0 bg-transparent p-0'
      )}
    >
      <div>
        <div data-testid="approval-tool" className="text-sm font-semibold">
          {displayToolName(approval.tool)}
        </div>
        {subtitle ? (
          <div
            data-testid="approval-subtitle"
            className="text-sm text-muted-foreground"
          >
            {subtitle}
          </div>
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
              {t('chat.approval.editedContentNote', {
                defaultValue:
                  'Edited content; the approved changes are in the diff.'
              })}
            </p>
          ) : null}
        </div>
      ) : null}

      <ApprovalDiff entries={diff} />

      {editMode === 'form' && fields ? (
        <div
          ref={editAreaRef}
          data-testid="approval-edit-form"
          className="mt-4 scroll-mt-4 border-t border-border pt-3"
        >
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t('chat.approval.editFields', { defaultValue: 'Edit fields' })}
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
              {t('chat.approval.saveAndApprove', {
                defaultValue: 'Save & approve'
              })}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelEdit}
            >
              {t('common.actions.cancel', { defaultValue: 'Cancel' })}
            </Button>
          </div>
        </div>
      ) : editMode === 'json' ? (
        <div ref={editAreaRef} className="mt-3 scroll-mt-4">
          <textarea
            ref={jsonEditorRef}
            aria-label={t('chat.approval.editedArgumentsJson', {
              defaultValue: 'Edited arguments JSON'
            })}
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
              {t('chat.approval.saveEdit', { defaultValue: 'Save edit' })}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelEdit}
            >
              {t('common.actions.cancel', { defaultValue: 'Cancel' })}
            </Button>
          </div>
        </div>
      ) : requestingChanges ? (
        <div className="mt-3">
          <label
            htmlFor={`approval-comment-${approval.id}`}
            className="text-xs font-medium text-muted-foreground"
          >
            {t('chat.approval.commentRequired', {
              defaultValue: 'What should change? (required)'
            })}
          </label>
          <textarea
            ref={commentRef}
            id={`approval-comment-${approval.id}`}
            aria-label={t('chat.approval.comment', {
              defaultValue: 'Comment'
            })}
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
              {t('chat.approval.requestChanges', {
                defaultValue: 'Request changes'
              })}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={cancelRequestChanges}
            >
              {t('common.actions.cancel', { defaultValue: 'Cancel' })}
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
            {t('chat.approval.approve', { defaultValue: 'Approve' })}
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
              {t('chat.approval.edit', { defaultValue: 'Edit' })}
            </Button>
          ) : null}
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => decide('reject')}
          >
            {t('chat.approval.reject', { defaultValue: 'Reject' })}
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
              {t('chat.approval.requestChanges', {
                defaultValue: 'Request changes'
              })}
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
