import type { IApprovalEditField } from '@chainlit-ng/react-client';

export type FieldValues = Record<string, string | boolean>;

export const isServerHeld = (field: IApprovalEditField): boolean =>
  field.serverHeld === true || field.secret === true;

export const initialFieldValues = (
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

export type EditValidationErrorKind = 'required' | 'wholeNumber' | 'number';

export type EditedArgumentsResult =
  | { ok: true; values: Record<string, unknown> }
  | { ok: false; kind: EditValidationErrorKind; field: string };

export const buildEditedArguments = (
  fields: IApprovalEditField[],
  values: FieldValues
): EditedArgumentsResult => {
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
      if (field.required) {
        return { ok: false, kind: 'required', field: field.label };
      }
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
        return { ok: false, kind: 'wholeNumber', field: field.label };
      }
      edited[field.name] = number;
      continue;
    }
    if (field.kind === 'number') {
      const number = Number(text);
      if (!Number.isFinite(number)) {
        return { ok: false, kind: 'number', field: field.label };
      }
      edited[field.name] = number;
      continue;
    }
    edited[field.name] = text;
  }
  return { ok: true, values: edited };
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
export const areArgumentsUnchanged = (
  fields: IApprovalEditField[],
  args: Record<string, unknown>,
  edited: Record<string, unknown>
): boolean => {
  const baseline = buildEditedArguments(
    fields,
    initialFieldValues(fields, args)
  );
  if (!baseline.ok) return false;
  return argumentsEqual(
    withServerHeldArguments(edited, fields, args),
    withServerHeldArguments(baseline.values, fields, args)
  );
};
