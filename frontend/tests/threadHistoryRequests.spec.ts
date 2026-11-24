import {
  beginThreadHistoryRequest,
  invalidateThreadHistoryRequests,
  isLatestThreadHistoryRequest
} from '@chainlit-ng/react-client';
import { describe, expect, it } from 'vitest';

describe('thread history request generation', () => {
  it('only treats the most recently started request as current', () => {
    const owner = {};
    const first = beginThreadHistoryRequest(owner);
    const second = beginThreadHistoryRequest(owner);

    expect(isLatestThreadHistoryRequest(owner, first)).toBe(false);
    expect(isLatestThreadHistoryRequest(owner, second)).toBe(true);
  });

  it('invalidates in-flight requests', () => {
    const owner = {};
    const request = beginThreadHistoryRequest(owner);

    invalidateThreadHistoryRequests(owner);

    expect(isLatestThreadHistoryRequest(owner, request)).toBe(false);
  });

  it('scopes generations to the owning client', () => {
    const firstOwner = {};
    const secondOwner = {};
    const request = beginThreadHistoryRequest(firstOwner);

    beginThreadHistoryRequest(secondOwner);

    expect(isLatestThreadHistoryRequest(firstOwner, request)).toBe(true);
  });
});
