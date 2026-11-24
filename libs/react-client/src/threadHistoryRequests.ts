interface ThreadHistoryRequestState {
  generation: number;
  firstPageRequestId: number | null;
}

const requestStates = new WeakMap<object, ThreadHistoryRequestState>();
const firstPageRefreshers = new WeakMap<object, () => Promise<void>>();

function getState(owner: object): ThreadHistoryRequestState {
  let state = requestStates.get(owner);
  if (!state) {
    state = { generation: 0, firstPageRequestId: null };
    requestStates.set(owner, state);
  }
  return state;
}

function nextGeneration(owner: object): number {
  const state = getState(owner);
  state.generation += 1;
  return state.generation;
}

/**
 * Start a first-page request. First-page requests take priority over
 * pagination: they bump the generation, invalidating any in-flight page
 * request, and mark a first-page request as in flight so new pagination
 * requests wait for it to finish.
 */
export function beginThreadHistoryRequest(owner: object): number {
  const state = getState(owner);
  const requestId = nextGeneration(owner);
  state.firstPageRequestId = requestId;
  return requestId;
}

/**
 * Start a pagination request. Pagination captures the current generation
 * without bumping it, so it can never invalidate an in-flight first-page
 * refresh. Returns `null` while a first-page request is in flight; the
 * caller should retry once the refreshed page info provides a new cursor.
 */
export function beginThreadHistoryPage(owner: object): number | null {
  const state = getState(owner);
  if (state.firstPageRequestId !== null) return null;
  return state.generation;
}

export function isLatestThreadHistoryRequest(
  owner: object,
  requestId: number
): boolean {
  return getState(owner).generation === requestId;
}

/**
 * Whether a first-page request is currently in flight for the owner.
 */
export function isThreadHistoryRefreshing(owner: object): boolean {
  const state = getState(owner);
  return (
    state.firstPageRequestId !== null &&
    state.firstPageRequestId === state.generation
  );
}

/**
 * Mark a first-page request as finished. Clears the in-flight marker only
 * when this request is still current, so a stale completion cannot unlock
 * pagination or loading state owned by a newer request.
 */
export function finishThreadHistoryRequest(
  owner: object,
  requestId: number
): void {
  const state = getState(owner);
  if (state.generation === requestId) {
    state.firstPageRequestId = null;
  }
}

export function invalidateThreadHistoryRequests(owner: object): void {
  const state = getState(owner);
  nextGeneration(owner);
  state.firstPageRequestId = null;
}

/**
 * Register the mounted sidebar's first-page refresh. Session events route
 * through this refresher so the mounted component owns loading state and
 * pagination coordination instead of racing a second request.
 */
export function registerThreadHistoryRefresher(
  owner: object,
  refresh: () => Promise<void>
): void {
  firstPageRefreshers.set(owner, refresh);
}

export function unregisterThreadHistoryRefresher(
  owner: object,
  refresh: () => Promise<void>
): void {
  if (firstPageRefreshers.get(owner) === refresh) {
    firstPageRefreshers.delete(owner);
  }
}

export function getThreadHistoryRefresher(
  owner: object
): (() => Promise<void>) | undefined {
  return firstPageRefreshers.get(owner);
}
