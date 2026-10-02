import { LoaderCircle } from 'lucide-react';
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cn } from './utils';
import { useMotionActivity } from '@/design/motion';

export type ButtonVariant = 'primary' | 'secondary' | 'quiet' | 'danger';
export type ButtonSize = 'small' | 'medium' | 'large';

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    children,
    className,
    disabled,
    leadingIcon,
    loading = false,
    size = 'medium',
    trailingIcon,
    type = 'button',
    variant = 'secondary',
    ...props
  },
  ref,
) {
  const motionActive = useMotionActivity();
  return (
    <button
      ref={ref}
      type={type}
      className={cn('ui-button', className)}
      data-size={size}
      data-variant={variant}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      data-motion-active={motionActive}
      {...props}
    >
      <span className="ui-button__icon" aria-hidden="true">
        {loading ? <LoaderCircle className="ui-spin" size={16} /> : leadingIcon}
      </span>
      {children ? <span className="ui-button__label">{children}</span> : null}
      {trailingIcon ? (
        <span className="ui-button__icon" aria-hidden="true">
          {trailingIcon}
        </span>
      ) : null}
      {loading ? <span aria-hidden="true" className="ui-button__progress" /> : null}
    </button>
  );
});
