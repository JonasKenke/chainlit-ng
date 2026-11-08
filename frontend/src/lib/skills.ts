export const SKILL_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const MAX_SKILL_DESCRIPTION_BYTES = 1024;
export const MAX_SKILL_BODY_BYTES = 64 * 1024;
export const MAX_SKILL_MARKDOWN_BYTES =
  MAX_SKILL_BODY_BYTES + MAX_SKILL_DESCRIPTION_BYTES + 4096;

export interface SkillInput {
  name: string;
  description: string;
  content: string;
}

export interface SkillFieldError {
  key: string;
  values?: Record<string, string | number>;
}

export interface SkillValidationErrors {
  name?: SkillFieldError;
  description?: SkillFieldError;
  content?: SkillFieldError;
}

export function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

export function validateSkill(input: SkillInput): SkillValidationErrors {
  const errors: SkillValidationErrors = {};
  const name = input.name.trim();
  const description = input.description.trim();

  if (!name) {
    errors.name = { key: 'chat.skills.validation.nameRequired' };
  } else if (!SKILL_NAME_PATTERN.test(name)) {
    errors.name = { key: 'chat.skills.validation.namePattern' };
  }

  const descriptionSize = utf8Bytes(description);
  if (!description) {
    errors.description = { key: 'chat.skills.validation.descriptionRequired' };
  } else if (/[\r\n]/.test(description)) {
    errors.description = {
      key: 'chat.skills.validation.descriptionSingleLine'
    };
  } else if (descriptionSize > MAX_SKILL_DESCRIPTION_BYTES) {
    errors.description = {
      key: 'chat.skills.validation.descriptionTooLarge',
      values: { size: descriptionSize, max: MAX_SKILL_DESCRIPTION_BYTES }
    };
  }

  const bodySize = utf8Bytes(input.content);
  if (bodySize > MAX_SKILL_BODY_BYTES) {
    errors.content = {
      key: 'chat.skills.validation.bodyTooLarge',
      values: { size: bodySize, max: MAX_SKILL_BODY_BYTES }
    };
  }

  return errors;
}

export function skillErrorDetail(error: unknown): string | undefined {
  if (error && typeof error === 'object' && 'detail' in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
  }
  return error instanceof Error && error.message ? error.message : undefined;
}

export function formatSkillTimestamp(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function downloadMarkdown(markdown: string, filename: string): void {
  const url = URL.createObjectURL(
    new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
