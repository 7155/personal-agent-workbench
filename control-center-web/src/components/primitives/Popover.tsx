import * as PopoverPrimitive from '@radix-ui/react-popover';
import { forwardRef, type ComponentPropsWithoutRef, type ComponentRef } from 'react';
import { useMotionActivity } from '@/design/motion';
import { cn } from './utils';

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverClose = PopoverPrimitive.Close;

export const PopoverContent = forwardRef<
  ComponentRef<typeof PopoverPrimitive.Content>,
  ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(function PopoverContent({ align = 'center', className, sideOffset = 6, ...props }, ref) {
  const callerMotionActive = 'data-motion-active' in props ? props['data-motion-active'] : undefined;
  const motionActive = useMotionActivity() && callerMotionActive !== false && callerMotionActive !== 'false';
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        ref={ref}
        align={align}
        className={cn('ui-popover', className)}
        sideOffset={sideOffset}
        collisionPadding={8}
        {...props}
        data-motion-active={motionActive}
      />
    </PopoverPrimitive.Portal>
  );
});
