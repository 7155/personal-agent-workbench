import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import type { ReactElement, ReactNode } from 'react';
import { useMotionActivity } from '@/design/motion';

export const TooltipProvider = TooltipPrimitive.Provider;

export function Tooltip({
  children,
  content,
  delayDuration = 350,
  side = 'right',
}: {
  children: ReactElement;
  content: ReactNode;
  delayDuration?: number;
  side?: 'top' | 'right' | 'bottom' | 'left';
}) {
  const motionActive = useMotionActivity();
  return (
    <TooltipPrimitive.Root delayDuration={delayDuration}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content className="ui-tooltip" side={side} sideOffset={6} data-motion-active={motionActive}>
          {content}
          <TooltipPrimitive.Arrow className="ui-tooltip__arrow" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
