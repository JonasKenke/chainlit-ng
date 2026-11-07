import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import Icon from '@/components/Icon';

const waitForIcon = async (container: HTMLElement, iconName: string) => {
  await waitFor(() =>
    expect(
      container.querySelector(`svg.lucide-${iconName}`)
    ).toBeInTheDocument()
  );
};

describe('Icon component', () => {
  it('renders icon with kebab-case name', async () => {
    const { container } = render(<Icon name="chevron-right" />);
    await waitForIcon(container, 'chevron-right');
  });

  it('renders icon with lowercase name', async () => {
    const { container } = render(<Icon name="plus" />);
    await waitForIcon(container, 'plus');
  });

  it('renders icon with PascalCase name', async () => {
    const { container } = render(<Icon name="ChevronRight" />);
    await waitForIcon(container, 'chevron-right');
  });

  it('renders icon with all uppercase name', async () => {
    const { container } = render(<Icon name="PLUS" />);
    await waitForIcon(container, 'plus');
  });

  it('renders icon with mixed case name', async () => {
    const { container } = render(<Icon name="ChEvRoN-rIgHt" />);
    await waitForIcon(container, 'chevron-right');
  });

  it('renders icon names that contain digits', async () => {
    const { container } = render(
      <>
        <Icon name="Grid2x2" />
        <Icon name="ArrowUp10" />
      </>
    );
    await waitForIcon(container, 'grid-2x2');
    await waitForIcon(container, 'arrow-up-1-0');
  });

  it('renders Lucide alias names', async () => {
    const { container } = render(
      <>
        <Icon name="MailIcon" />
        <Icon name="LucideMail" />
      </>
    );
    await waitFor(() =>
      expect(container.querySelectorAll('svg.lucide-mail')).toHaveLength(2)
    );
  });

  it('returns null and warns for invalid icon name', () => {
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { container } = render(<Icon name="invalid-icon-name" />);

    expect(container.firstChild).toBeNull();
    expect(consoleSpy).toHaveBeenCalledWith(
      'Icon "invalid-icon-name" not found in Lucide icons'
    );

    consoleSpy.mockRestore();
  });

  it('reserves the icon box while the icon chunk loads', () => {
    const { container } = render(
      <Icon name="chevron-right" size={20} className="test-class" />
    );
    const placeholder = container.querySelector('[data-icon-placeholder]');

    expect(placeholder).toBeInTheDocument();
    expect(placeholder).toHaveClass('test-class');
    expect(placeholder).toHaveAttribute('width', '20');
    expect(placeholder).toHaveAttribute('height', '20');
  });

  it('passes props to the icon component', async () => {
    const { container } = render(
      <Icon name="home" size={24} color="red" className="test-class" />
    );
    await waitFor(() =>
      expect(container.querySelector('svg.lucide-home')).toBeInTheDocument()
    );
    const svg = container.querySelector('svg.lucide-home');

    expect(svg).toHaveClass('test-class');
    expect(svg).toHaveAttribute('width', '24');
    expect(svg).toHaveAttribute('height', '24');
    expect(svg).toHaveAttribute('stroke', 'red');
  });
});
