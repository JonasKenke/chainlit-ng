const requestGenerations = new WeakMap<object, number>();

function nextGeneration(owner: object): number {
  const next = (requestGenerations.get(owner) ?? 0) + 1;
  requestGenerations.set(owner, next);
  return next;
}

export function beginThreadHistoryRequest(owner: object): number {
  return nextGeneration(owner);
}

export function isLatestThreadHistoryRequest(
  owner: object,
  requestId: number
): boolean {
  return (requestGenerations.get(owner) ?? 0) === requestId;
}

export function invalidateThreadHistoryRequests(owner: object): void {
  nextGeneration(owner);
}
