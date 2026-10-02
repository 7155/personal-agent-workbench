import * as RadioGroup from '@radix-ui/react-radio-group';
import { useId, type ReactNode } from 'react';
import { LayoutGroup, motion } from 'motion/react';
import { useMotionActivity } from '@/design/motion';
import { cn } from './utils';

export type SegmentedControlItem<T extends string> = {
  value: T;
  label: ReactNode;
  disabled?: boolean;
};

export function SegmentedControl<T extends string>({
  'aria-label': ariaLabel,
  className,
  disabled,
  items,
  onValueChange,
  value,
}: {
  'aria-label': string;
  className?: string;
  disabled?: boolean;
  items: readonly SegmentedControlItem<T>[];
  onValueChange: (value: T) => void;
  value: T;
}) {
  const motionId = useId();
  const motionActive = useMotionActivity();
  return (
    <LayoutGroup id={motionId}>
    <RadioGroup.Root
      aria-label={ariaLabel}
      className={cn('ui-segmented', className)}
      data-motion-active={motionActive}
      data-motion-group={motionId}
      disabled={disabled}
      onValueChange={(next) => onValueChange(next as T)}
      orientation="horizontal"
      value={value}
    >
      {items.map((item) => (
        <RadioGroup.Item
          key={item.value}
          className="ui-segmented__item"
          disabled={item.disabled}
          value={item.value}
        >
          {value === item.value ? motionActive ? <motion.span
            aria-hidden="true"
            className="ui-segmented__selection"
            initial={false}
            layoutDependency={value}
            layoutId="selected-segment"
            transition={{ type: 'spring', stiffness: 520, damping: 42, mass: 0.55 }}
          /> : <span aria-hidden="true" className="ui-segmented__selection" /> : null}
          {item.label}
        </RadioGroup.Item>
      ))}
    </RadioGroup.Root>
    </LayoutGroup>
  );
}
