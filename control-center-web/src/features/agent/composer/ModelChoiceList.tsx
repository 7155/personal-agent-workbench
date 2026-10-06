import { Check } from 'lucide-react';
import { type KeyboardEvent, type Ref } from 'react';

import { ProviderMark } from '../marks/ConversationMarks';
import {
  firstModelChoiceKey,
  type ModelChoiceGroup,
  type ModelChoiceOption,
} from './model-choice';

/**
 * The one way PAWOS asks "which model".
 *
 * Every model is on the surface at once under a quiet provider heading, the
 * provider mark carries identity after the name is gone, and the current
 * choice is confirmed by a check rather than by a collapsed field. Arrow keys
 * walk the whole catalog, so picking never turns into navigating.
 *
 * Presentation only: the list holds no selection state and never talks to
 * Runtime. Its callers own what a choice means.
 */
export function ModelChoiceList({
  ariaLabel,
  className = 'agent-model-picker__list',
  disabled = false,
  emptyLabel = '当前没有可用模型',
  groups,
  leadingOptions = [],
  listRef,
  onChoose,
  selectedKey,
}: {
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  emptyLabel?: string;
  groups: readonly ModelChoiceGroup[];
  /** Ungrouped rows above the catalog — a surface's "let Runtime decide". */
  leadingOptions?: readonly ModelChoiceOption[];
  listRef?: Ref<HTMLDivElement>;
  onChoose: (option: ModelChoiceOption) => void;
  selectedKey: string;
}) {
  const firstKey = leadingOptions[0]?.key ?? firstModelChoiceKey(groups);
  const options = [
    ...leadingOptions,
    ...groups.flatMap((group) => group.options),
  ];
  const anySelected = options.some((option) => option.key === selectedKey);

  function optionRow(option: ModelChoiceOption, marked: boolean) {
    const selected = option.key === selectedKey;
    return (
      <button
        aria-label={`选择模型 ${option.name}`}
        aria-selected={selected}
        disabled={disabled}
        key={option.key}
        onClick={() => onChoose(option)}
        role="option"
        tabIndex={(anySelected ? selected : option.key === firstKey) ? 0 : -1}
        type="button"
      >
        {marked ? (
          <ProviderMark
            displayName={option.providerName}
            providerId={option.providerId}
            size={17}
          />
        ) : (
          <span aria-hidden="true" className="agent-model-picker__auto-mark" />
        )}
        <span>
          <strong>{option.name}</strong>
          <small>{option.detail}</small>
        </span>
        <Check aria-hidden="true" className="agent-model-picker__check" size={15} />
      </button>
    );
  }

  return (
    <div
      aria-label={ariaLabel}
      className={className}
      onKeyDown={moveOptionFocus}
      ref={listRef}
      role="listbox"
    >
      {leadingOptions.map((option) => optionRow(option, false))}
      {groups.map((group) => (
        <div
          aria-label={group.displayName}
          className="agent-model-picker__group"
          key={group.providerId}
          role="group"
        >
          <p aria-hidden="true" className="agent-model-picker__group-name">
            <span>{group.displayName}</span>
            <small>{group.options.length}</small>
          </p>
          {group.options.map((option) => optionRow(option, true))}
        </div>
      ))}
      {options.length ? null : <p className="agent-model-picker__empty">{emptyLabel}</p>}
    </div>
  );
}

export function moveOptionFocus(event: KeyboardEvent<HTMLDivElement>): void {
  moveButtonFocus(event, '[role="option"]');
}

export function moveButtonFocus(
  event: KeyboardEvent<HTMLDivElement>,
  selector: string,
): void {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
  const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>(selector));
  if (options.length === 0) return;
  event.preventDefault();
  const current = options.findIndex((option) => option === document.activeElement);
  const next = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? options.length - 1
      : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
        ? Math.max(0, (current < 0 ? 0 : current) - 1)
        : Math.min(options.length - 1, (current < 0 ? -1 : current) + 1);
  options[next]?.focus();
}
