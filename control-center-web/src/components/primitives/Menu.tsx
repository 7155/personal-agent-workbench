import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Check, ChevronRight } from 'lucide-react';
import { forwardRef, type ComponentPropsWithoutRef, type ComponentRef } from 'react';
import { useMotionActivity } from '@/design/motion';
import { cn } from './utils';

export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;
export const MenuGroup = DropdownMenu.Group;
export const MenuRadioGroup = DropdownMenu.RadioGroup;
export const MenuSub = DropdownMenu.Sub;

export const MenuContent = forwardRef<
  ComponentRef<typeof DropdownMenu.Content>,
  ComponentPropsWithoutRef<typeof DropdownMenu.Content>
>(function MenuContent({ className, sideOffset = 6, ...props }, ref) {
  const callerMotionActive = 'data-motion-active' in props ? props['data-motion-active'] : undefined;
  const motionActive = useMotionActivity() && callerMotionActive !== false && callerMotionActive !== 'false';
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        ref={ref}
        className={cn('ui-menu', className)}
        sideOffset={sideOffset}
        collisionPadding={8}
        {...props}
        data-motion-active={motionActive}
      />
    </DropdownMenu.Portal>
  );
});

export const MenuItem = forwardRef<
  ComponentRef<typeof DropdownMenu.Item>,
  ComponentPropsWithoutRef<typeof DropdownMenu.Item> & { inset?: boolean }
>(function MenuItem({ className, inset, ...props }, ref) {
  return (
    <DropdownMenu.Item
      ref={ref}
      className={cn('ui-menu__item', inset && 'ui-menu__item--inset', className)}
      {...props}
    />
  );
});

export const MenuCheckboxItem = forwardRef<
  ComponentRef<typeof DropdownMenu.CheckboxItem>,
  ComponentPropsWithoutRef<typeof DropdownMenu.CheckboxItem>
>(function MenuCheckboxItem({ children, className, ...props }, ref) {
  return (
    <DropdownMenu.CheckboxItem ref={ref} className={cn('ui-menu__item', className)} {...props}>
      <span className="ui-menu__indicator">
        <DropdownMenu.ItemIndicator>
          <Check size={14} />
        </DropdownMenu.ItemIndicator>
      </span>
      {children}
    </DropdownMenu.CheckboxItem>
  );
});

export const MenuRadioItem = forwardRef<
  ComponentRef<typeof DropdownMenu.RadioItem>,
  ComponentPropsWithoutRef<typeof DropdownMenu.RadioItem>
>(function MenuRadioItem({ children, className, ...props }, ref) {
  return (
    <DropdownMenu.RadioItem ref={ref} className={cn('ui-menu__item', className)} {...props}>
      <span className="ui-menu__indicator">
        <DropdownMenu.ItemIndicator>
          <Check size={14} />
        </DropdownMenu.ItemIndicator>
      </span>
      {children}
    </DropdownMenu.RadioItem>
  );
});

export const MenuLabel = forwardRef<
  ComponentRef<typeof DropdownMenu.Label>,
  ComponentPropsWithoutRef<typeof DropdownMenu.Label>
>(function MenuLabel({ className, ...props }, ref) {
  return <DropdownMenu.Label ref={ref} className={cn('ui-menu__label', className)} {...props} />;
});

export const MenuSeparator = forwardRef<
  ComponentRef<typeof DropdownMenu.Separator>,
  ComponentPropsWithoutRef<typeof DropdownMenu.Separator>
>(function MenuSeparator({ className, ...props }, ref) {
  return <DropdownMenu.Separator ref={ref} className={cn('ui-menu__separator', className)} {...props} />;
});

export const MenuSubTrigger = forwardRef<
  ComponentRef<typeof DropdownMenu.SubTrigger>,
  ComponentPropsWithoutRef<typeof DropdownMenu.SubTrigger>
>(function MenuSubTrigger({ children, className, ...props }, ref) {
  return (
    <DropdownMenu.SubTrigger ref={ref} className={cn('ui-menu__item', className)} {...props}>
      {children}
      <ChevronRight className="ui-menu__trailing" size={14} />
    </DropdownMenu.SubTrigger>
  );
});

export const MenuSubContent = forwardRef<
  ComponentRef<typeof DropdownMenu.SubContent>,
  ComponentPropsWithoutRef<typeof DropdownMenu.SubContent>
>(function MenuSubContent({ className, ...props }, ref) {
  const callerMotionActive = 'data-motion-active' in props ? props['data-motion-active'] : undefined;
  const motionActive = useMotionActivity() && callerMotionActive !== false && callerMotionActive !== 'false';
  return <DropdownMenu.SubContent ref={ref} className={cn('ui-menu', className)} {...props} data-motion-active={motionActive} />;
});
