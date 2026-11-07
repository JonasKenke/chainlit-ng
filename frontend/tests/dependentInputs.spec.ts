import { describe, expect, it } from 'vitest';

import {
  type DependentInput,
  effectiveItems,
  resolveDependentValues
} from '../src/components/ChatSettings/dependentInputs';

const fallbackItems = [{ label: 'Other', value: 'other' }];

const city: DependentInput = {
  id: 'city',
  type: 'select',
  items: fallbackItems,
  depends_on: 'country',
  dependent_items: {
    US: [
      { label: 'New York', value: 'ny' },
      { label: 'San Francisco', value: 'sf' }
    ],
    CA: [{ label: 'Toronto', value: 'toronto' }]
  }
};

const country: DependentInput = {
  id: 'country',
  type: 'select',
  items: [
    { label: 'United States', value: 'US' },
    { label: 'Canada', value: 'CA' }
  ]
};

describe('effectiveItems', () => {
  it('returns the dependent items for a known parent value', () => {
    expect(effectiveItems(city, 'US')).toEqual([
      { label: 'New York', value: 'ny' },
      { label: 'San Francisco', value: 'sf' }
    ]);
  });

  it('falls back to items for an unknown or missing parent value', () => {
    expect(effectiveItems(city, 'MX')).toEqual(fallbackItems);
    expect(effectiveItems(city, undefined)).toEqual(fallbackItems);
  });
});

describe('resolveDependentValues', () => {
  it('clears a single-value child whose value is no longer available', () => {
    expect(
      resolveDependentValues([country, city], {
        country: 'CA',
        city: 'ny',
        temperature: 1
      })
    ).toEqual({ country: 'CA', city: '', temperature: 1 });
  });

  it('clears a radio child whose value is no longer available', () => {
    const radio: DependentInput = { ...city, id: 'radio', type: 'radio' };

    expect(
      resolveDependentValues([country, radio], {
        country: 'CA',
        radio: 'ny'
      })
    ).toEqual({ country: 'CA', radio: '' });
  });

  it('intersects multiselect children, keeping still-valid values', () => {
    const cities: DependentInput = {
      ...city,
      id: 'cities',
      type: 'multiselect'
    };

    expect(
      resolveDependentValues([country, cities], {
        country: 'CA',
        cities: ['ny', 'toronto'],
        temperature: 1
      })
    ).toEqual({ country: 'CA', cities: ['toronto'], temperature: 1 });
  });

  it('reconciles a two-level chain parent-first', () => {
    const district: DependentInput = {
      id: 'district',
      type: 'select',
      items: fallbackItems,
      depends_on: 'city',
      dependent_items: {
        ny: [{ label: 'Manhattan', value: 'manhattan' }],
        toronto: [{ label: 'North York', value: 'north-york' }]
      }
    };

    expect(
      resolveDependentValues([country, city, district], {
        country: 'CA',
        city: 'ny',
        district: 'manhattan'
      })
    ).toEqual({ country: 'CA', city: '', district: '' });
  });

  it('leaves inputs without dependencies untouched', () => {
    expect(
      resolveDependentValues([country, city], {
        country: 'US',
        city: 'ny',
        temperature: 1
      })
    ).toEqual({ country: 'US', city: 'ny', temperature: 1 });
  });
});
