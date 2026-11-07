import { Check } from 'lucide-react';
import { useMemo } from 'react';

export default function CountLabel({ count }) {
  const label = useMemo(() => `Count: ${count}`, [count]);

  return (
    <div>
      {label} <Check id="helper-icon" />
    </div>
  );
}
