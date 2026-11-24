import {
  beginThreadHistoryPage,
  beginThreadHistoryRequest,
  finishThreadHistoryRequest,
  getThreadHistoryRefresher,
  invalidateThreadHistoryRequests,
  isLatestThreadHistoryRequest,
  isThreadHistoryRefreshing,
  registerThreadHistoryRefresher,
  unregisterThreadHistoryRefresher
} from '@chainlit-ng/react-client';
import { describe, expect, it, vi } from 'vitest';

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

describe('thread history pagination and refresh coordination', () => {
  it('lets pagination share the generation without invalidating a refresh', () => {
    const owner = {};
    const refresh = beginThreadHistoryRequest(owner);
    finishThreadHistoryRequest(owner, refresh);

    const page = beginThreadHistoryPage(owner);

    expect(page).not.toBeNull();
    expect(isThreadHistoryRefreshing(owner)).toBe(false);
    expect(isLatestThreadHistoryRequest(owner, page!)).toBe(true);
  });

  it('blocks pagination while a first-page refresh is in flight', () => {
    const owner = {};

    beginThreadHistoryRequest(owner);

    expect(isThreadHistoryRefreshing(owner)).toBe(true);
    expect(beginThreadHistoryPage(owner)).toBeNull();
  });

  it('rejects an older page after a newer refresh starts', () => {
    const owner = {};
    const page = beginThreadHistoryPage(owner);
    expect(page).not.toBeNull();

    beginThreadHistoryRequest(owner);

    expect(isLatestThreadHistoryRequest(owner, page!)).toBe(false);
    expect(isThreadHistoryRefreshing(owner)).toBe(true);
  });

  it('does not let a stale completion clear the in-flight refresh', () => {
    const owner = {};
    const stale = beginThreadHistoryRequest(owner);
    beginThreadHistoryRequest(owner);

    finishThreadHistoryRequest(owner, stale);

    expect(isThreadHistoryRefreshing(owner)).toBe(true);
    expect(beginThreadHistoryPage(owner)).toBeNull();
  });

  it('clears the in-flight refresh when the current request finishes', () => {
    const owner = {};
    const current = beginThreadHistoryRequest(owner);

    finishThreadHistoryRequest(owner, current);

    expect(isThreadHistoryRefreshing(owner)).toBe(false);
    expect(beginThreadHistoryPage(owner)).not.toBeNull();
  });

  it('clears the in-flight refresh on invalidation', () => {
    const owner = {};
    beginThreadHistoryRequest(owner);

    invalidateThreadHistoryRequests(owner);

    expect(isThreadHistoryRefreshing(owner)).toBe(false);
  });
});

describe('thread history refresher registration', () => {
  it('registers and retrieves the refresher for an owner', () => {
    const owner = {};
    const refresh = vi.fn().mockResolvedValue(undefined);

    registerThreadHistoryRefresher(owner, refresh);

    expect(getThreadHistoryRefresher(owner)).toBe(refresh);
  });

  it('unregisters only the matching refresher', () => {
    const owner = {};
    const first = vi.fn().mockResolvedValue(undefined);
    const second = vi.fn().mockResolvedValue(undefined);

    registerThreadHistoryRefresher(owner, first);
    registerThreadHistoryRefresher(owner, second);
    unregisterThreadHistoryRefresher(owner, first);

    expect(getThreadHistoryRefresher(owner)).toBe(second);

    unregisterThreadHistoryRefresher(owner, second);

    expect(getThreadHistoryRefresher(owner)).toBeUndefined();
  });

  it('scopes refreshers to the owning client', () => {
    const first = {};
    const second = {};
    const refresh = vi.fn().mockResolvedValue(undefined);

    registerThreadHistoryRefresher(first, refresh);

    expect(getThreadHistoryRefresher(second)).toBeUndefined();
  });
});
