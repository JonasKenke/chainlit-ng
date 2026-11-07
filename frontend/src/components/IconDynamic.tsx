import { DynamicIcon, dynamicIconImports } from 'lucide-react/dynamic';
import type { IconName } from 'lucide-react/dynamic';
import type { ReactElement } from 'react';

interface Props {
  name: string;
  className?: string;
  size?: number;
  color?: string;
  placeholder: ReactElement;
}

// Convert the name to proper case
const formatIconName = (name: string): string => {
  //aggressively lowercase the parts to clean up inputs like "ChEvRoN-rIgHt"
  if (name.includes('-')) {
    return name
      .split('-')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
      .join('');
  }
  if (name === name.toUpperCase()) {
    return name.charAt(0).toUpperCase() + name.slice(1).toLowerCase();
  }
  return name.charAt(0).toUpperCase() + name.slice(1);
};

// Lucide's dynamic registry is keyed by kebab-case names while call sites pass
// PascalCase export names ("ChevronRight") or kebab-case ("chevron-right").
const iconNames = new Map<string, IconName>(
  (Object.keys(dynamicIconImports) as IconName[]).map(
    (iconName) => [formatIconName(iconName), iconName] as const
  )
);

const resolveIconName = (name: string): IconName | undefined => {
  const formattedName = formatIconName(name);
  // Lucide also exports "MailIcon" and "LucideMail" aliases for "Mail".
  return (
    iconNames.get(formattedName) ??
    iconNames.get(formattedName.replace(/Icon$/, '')) ??
    iconNames.get(formattedName.replace(/^Lucide/, '')) ??
    iconNames.get(formattedName.replace(/^Lucide/, '').replace(/Icon$/, ''))
  );
};

const IconDynamic = ({ name, className, size, color, placeholder }: Props) => {
  const iconName = resolveIconName(name);

  if (!iconName) {
    console.warn(`Icon "${name}" not found in Lucide icons`);
    return null;
  }

  return (
    <DynamicIcon
      name={iconName}
      className={className}
      size={size}
      color={color}
      fallback={() => placeholder}
    />
  );
};

export default IconDynamic;
