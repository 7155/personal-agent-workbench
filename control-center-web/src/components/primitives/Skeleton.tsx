import type { HTMLAttributes } from 'react';
import { cn } from './utils';
import { useMotionActivity } from '@/design/motion';

export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  const motionActive = useMotionActivity();
  return <div aria-hidden="true" className={cn('ui-skeleton', className)} data-motion-active={motionActive} {...props} />;
}
