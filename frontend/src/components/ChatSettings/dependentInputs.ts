export interface DependentInputItem {
  label: string;
  value: string | number;
}

export interface DependentInput {
  id: string;
  type?: string;
  items?: DependentInputItem[];
  depends_on?: string;
  dependent_items?: Record<string, DependentInputItem[]>;
  inputs?: DependentInput[];
}

type SettingsValues = Record<string, unknown>;

const flattenInputs = (inputs: DependentInput[]): DependentInput[] =>
  inputs.flatMap((input) =>
    Array.isArray(input?.inputs) && input.inputs.length > 0
      ? flattenInputs(input.inputs)
      : [input]
  );

/**
 * Options to render for an input. A known parent value selects its dependent
 * items; anything else falls back to the input's static items.
 */
export function effectiveItems(
  input: DependentInput,
  parentValue: unknown
): DependentInputItem[] {
  const dependentItems = input.dependent_items;
  if (
    typeof parentValue === 'string' &&
    dependentItems &&
    Object.prototype.hasOwnProperty.call(dependentItems, parentValue)
  ) {
    return dependentItems[parentValue] ?? [];
  }
  return input.items ?? [];
}

/**
 * Reconciles dependent values with the option set of their parent, parents
 * first. Single-value children are cleared when their value is no longer
 * available; multiselect children keep only the still-available values.
 */
export function resolveDependentValues<T extends SettingsValues>(
  inputs: DependentInput[],
  values: T
): T {
  const leaves = flattenInputs(inputs);
  const byId = new Map(leaves.map((input) => [input.id, input]));

  const depth = (input: DependentInput, seen = new Set<string>()): number => {
    const parent = input.depends_on ? byId.get(input.depends_on) : undefined;
    if (!parent || seen.has(input.id)) return 0;
    seen.add(input.id);
    return 1 + depth(parent, seen);
  };

  const children = leaves
    .filter((input) => input.depends_on)
    .sort((a, b) => depth(a) - depth(b));

  const resolved: SettingsValues = { ...values };

  for (const input of children) {
    const allowedValues = new Set(
      effectiveItems(input, resolved[input.depends_on!]).map(
        (item) => item.value
      )
    );
    const current = resolved[input.id];

    if (input.type === 'multiselect') {
      if (Array.isArray(current)) {
        const intersection = current.filter((value) =>
          allowedValues.has(value)
        );
        if (intersection.length !== current.length) {
          resolved[input.id] = intersection;
        }
      }
    } else if (input.type === 'select' || input.type === 'radio') {
      if (!allowedValues.has(current as DependentInputItem['value'])) {
        resolved[input.id] = '';
      }
    }
  }

  return resolved as T;
}
