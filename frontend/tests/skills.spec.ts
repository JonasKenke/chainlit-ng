import {
  MAX_SKILL_BODY_BYTES,
  MAX_SKILL_DESCRIPTION_BYTES,
  validateSkill
} from '@/lib/skills';
import { describe, expect, it } from 'vitest';

const valid = {
  name: 'release-notes',
  description: 'Draft user-facing release notes from a changelog.',
  content: '# Release notes\n\nList the changes.'
};

describe('validateSkill', () => {
  it('accepts a valid skill', () => {
    expect(validateSkill(valid)).toEqual({});
  });

  it('requires a name that matches the backend pattern', () => {
    expect(validateSkill({ ...valid, name: '' }).name?.key).toBe(
      'chat.skills.validation.nameRequired'
    );
    expect(validateSkill({ ...valid, name: 'Release Notes' }).name?.key).toBe(
      'chat.skills.validation.namePattern'
    );
    expect(validateSkill({ ...valid, name: '_skill' }).name?.key).toBe(
      'chat.skills.validation.namePattern'
    );
    expect(
      validateSkill({ ...valid, name: `a${'b'.repeat(64)}` }).name?.key
    ).toBe('chat.skills.validation.namePattern');
  });

  it('requires a single-line description within the byte cap', () => {
    expect(validateSkill({ ...valid, description: '' }).description?.key).toBe(
      'chat.skills.validation.descriptionRequired'
    );
    expect(
      validateSkill({ ...valid, description: 'one\ntwo' }).description?.key
    ).toBe('chat.skills.validation.descriptionSingleLine');
    expect(
      validateSkill({
        ...valid,
        description: 'a'.repeat(MAX_SKILL_DESCRIPTION_BYTES + 1)
      }).description?.key
    ).toBe('chat.skills.validation.descriptionTooLarge');
  });

  it('counts UTF-8 bytes for the description cap', () => {
    expect(
      validateSkill({
        ...valid,
        description: 'é'.repeat(MAX_SKILL_DESCRIPTION_BYTES / 2)
      })
    ).toEqual({});
    expect(
      validateSkill({
        ...valid,
        description: 'é'.repeat(MAX_SKILL_DESCRIPTION_BYTES / 2 + 1)
      }).description?.key
    ).toBe('chat.skills.validation.descriptionTooLarge');
  });

  it('rejects a body over 64 KiB but accepts the boundary', () => {
    expect(
      validateSkill({ ...valid, content: 'a'.repeat(MAX_SKILL_BODY_BYTES) })
    ).toEqual({});
    expect(
      validateSkill({
        ...valid,
        content: 'a'.repeat(MAX_SKILL_BODY_BYTES + 1)
      }).content?.key
    ).toBe('chat.skills.validation.bodyTooLarge');
  });
});
