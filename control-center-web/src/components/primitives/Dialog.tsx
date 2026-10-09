import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import {
  createContext,
  forwardRef,
  useContext,
  useRef,
  type ComponentPropsWithoutRef,
  type ComponentRef,
  type HTMLAttributes,
  type MutableRefObject,
} from 'react';
import { useMotionActivity } from '@/design/motion';
import { IconButton } from './IconButton';
import { cn } from './utils';

type DialogRootProps = ComponentPropsWithoutRef<typeof DialogPrimitive.Root>;
type DialogContentProps = ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & { hideClose?: boolean };

const DialogOpenerContext = createContext<MutableRefObject<HTMLElement | null> | null>(null);

/**
 * Radix restores focus automatically when a DialogTrigger is present. Many
 * product workflows are controlled dialogs opened from an existing list row,
 * though, so there is no trigger for Radix to remember. Keep the active
 * control per dialog and let DialogContent return focus after dismissal.
 */
export function Dialog(props: DialogRootProps) {
  const openerRef = useRef<HTMLElement | null>(null);
  return (
    <DialogOpenerContext.Provider value={openerRef}>
      <DialogPrimitive.Root {...props} />
    </DialogOpenerContext.Provider>
  );
}
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export const DialogContent = forwardRef<
  ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(function DialogContent(
  {
    children,
    className,
    hideClose = false,
    onCloseAutoFocus,
    onOpenAutoFocus,
    ...props
  },
  ref,
) {
  const openerRef = useContext(DialogOpenerContext);
  const callerMotionActive = 'data-motion-active' in props ? props['data-motion-active'] : undefined;
  const motionActive = useMotionActivity() && callerMotionActive !== false && callerMotionActive !== 'false';
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="ui-dialog__overlay" data-motion-active={motionActive} />
      <DialogPrimitive.Content
        ref={ref}
        className={cn('ui-dialog', className)}
        onOpenAutoFocus={(event) => {
          const activeElement = document.activeElement;
          if (activeElement instanceof HTMLElement && activeElement !== document.body) {
            if (openerRef) openerRef.current = activeElement;
          }
          onOpenAutoFocus?.(event);
        }}
        onCloseAutoFocus={(event) => {
          onCloseAutoFocus?.(event);
          if (event.defaultPrevented) return;
          const opener = openerRef?.current;
          if (!opener?.isConnected) return;
          event.preventDefault();
          opener.focus({ preventScroll: true });
          if (openerRef) openerRef.current = null;
        }}
        {...props}
        data-motion-active={motionActive}
      >
        {children}
        {!hideClose ? (
          <DialogPrimitive.Close asChild>
            <IconButton className="ui-dialog__close" icon={<X size={17} />} label="关闭" />
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});

export const DialogTitle = forwardRef<
  ComponentRef<typeof DialogPrimitive.Title>,
  ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function DialogTitle({ className, ...props }, ref) {
  return <DialogPrimitive.Title ref={ref} className={cn('ui-dialog__title', className)} {...props} />;
});

export const DialogDescription = forwardRef<
  ComponentRef<typeof DialogPrimitive.Description>,
  ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function DialogDescription({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Description
      ref={ref}
      className={cn('ui-dialog__description', className)}
      {...props}
    />
  );
});

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('ui-dialog__header', className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('ui-dialog__footer', className)} {...props} />;
}
