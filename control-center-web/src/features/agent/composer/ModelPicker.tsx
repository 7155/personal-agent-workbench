import './model-picker.css';
import { ChevronDown, ChevronRight, LoaderCircle, Search } from 'lucide-react';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from 'react';

import {
  Button,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/primitives';
import { ProviderMark } from '../marks/ConversationMarks';
import { modelSelectionFromCatalog } from '../model-selection';
import { supportedPiThinkingLevels, type PiModelOption } from '../model-catalog-options';
import type { ModelCatalog, ThinkingLevel } from '../types';
import { ModelChoiceList, moveButtonFocus } from './ModelChoiceList';
import {
  filterModelChoiceGroups,
  modelChoiceGroupsFromCatalog,
  modelChoiceGroupsFromPiOptions,
  modelChoiceKey,
} from './model-choice';

/**
 * Model and reasoning commit through the same Pi selection contract, so the
 * composer carries them as one control: a single trigger naming both facts,
 * opening a compact reasoning control. Model search is disclosed on request;
 * the same component serves both new work and the live Session composer.
 */
export function ModelPicker({
  catalog,
  options,
  className,
  onOpen,
  disabled,
  pending,
  requestOpen,
  thinkingRequestOpen = 0,
  onChange,
}: {
  catalog?: ModelCatalog;
  options?: { models: PiModelOption[]; modelReference: string; thinking: string };
  className?: string;
  onOpen?: () => void;
  disabled: boolean;
  pending: boolean;
  requestOpen: number;
  thinkingRequestOpen?: number;
  onChange: (provider: string, modelId: string, level: ThinkingLevel) => void;
}) {
  const [open, setOpen] = useState(false);
  const [focusSection, setFocusSection] = useState<'model' | 'thinking'>('thinking');
  const [query, setQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const reasoningRef = useRef<HTMLDivElement>(null);
  const optionModel = options?.models.find((model) => model.reference === options.modelReference);
  const selection = options
    ? optionModel && { provider: optionModel.provider, modelId: optionModel.id, level: options.thinking }
    : catalog ? modelSelectionFromCatalog(catalog) : undefined;
  const selectedProvider = catalog?.providers.find(
    (item) => item.id === selection?.provider,
  );
  const selectedModel = optionModel ?? selectedProvider?.models.find(
    (item) => item.id === selection?.modelId,
  );
  const providerName = selectedProvider?.displayName || selectedModel?.provider || '';
  const selectedLabel = selectedModel
    ? `${selectedModel.name} · ${providerName}`
    : '未选择';
  const levels = supportedPiThinkingLevels(selectedModel ? {
    ...selectedModel, reference: '',
  } : undefined, { includeOff: true }) as ThinkingLevel[];
  const thinking = preferredThinkingLevel(levels, (selection?.level ?? catalog?.thinkingLevel ?? 'off') as ThinkingLevel);
  const groups = useMemo(() => options ? modelChoiceGroupsFromPiOptions(options.models) : modelChoiceGroupsFromCatalog(catalog), [catalog, options?.models]);
  const filteredGroups = useMemo(
    () => filterModelChoiceGroups(groups, query),
    [groups, query],
  );
  const selectedKey = options?.modelReference ?? (selection ? modelChoiceKey(selection.provider, selection.modelId) : '');
  const modelRequestRef = useRef(0);
  const thinkingRequestRef = useRef(0);

  useEffect(() => {
    if (requestOpen <= modelRequestRef.current || (!catalog && !options) || disabled) return;
    modelRequestRef.current = requestOpen;
    setQuery('');
    setFocusSection('model');
    setOpen(true);
  }, [catalog, disabled, requestOpen]);

  useEffect(() => {
    if (thinkingRequestOpen <= thinkingRequestRef.current || (!catalog && !options) || disabled || levels.length === 0) return;
    thinkingRequestRef.current = thinkingRequestOpen;
    setFocusSection('thinking');
    setOpen(true);
  }, [catalog, disabled, levels.length, thinkingRequestOpen]);

  useEffect(() => {
    if (!open || focusSection !== 'model') return undefined;
    const frame = window.requestAnimationFrame(() => searchRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [focusSection, open]);

  useEffect(() => {
    if (!open || focusSection !== 'thinking') return undefined;
    const frame = window.requestAnimationFrame(() => {
      const selected = reasoningRef.current?.querySelector<HTMLButtonElement>(
        '[role="radio"][aria-checked="true"]',
      );
      selected?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusSection, open]);

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (nextOpen) {
          setQuery('');
          setFocusSection(selectedModel && levels.length ? 'thinking' : 'model');
          onOpen?.();
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          aria-busy={pending || undefined}
          aria-label={`模型与推理：${selectedLabel} · ${thinkingLabel(thinking)}`}
          className={className ?? "agent-composer__picker"}
          disabled={(!catalog && !options) || disabled}
          leadingIcon={pending
            ? <LoaderCircle className="ui-spin" size={15} />
            : (
              <ProviderMark
                displayName={selectedProvider?.displayName}
                providerId={selectedProvider?.id ?? selection?.provider}
                size={16}
              />
            )}
          size="small"
          title={`模型与推理：${selectedLabel} · ${thinkingLabel(thinking)}`}
          variant="quiet"
        >
          <span className="agent-composer__picker-text">
            {selectedModel ? selectedModel.name : '选择模型'}
          </span>
          <span className="agent-composer__picker-thinking">{compactThinkingLabel(thinking)}</span>
          <ChevronDown className="caret" size={12} aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        aria-label="选择模型与推理强度"
        className="agent-model-picker"
        data-catalog-open={focusSection === 'model' || undefined}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          if (focusSection === 'model') searchRef.current?.focus();
          else reasoningRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
        }}
      >
        <button
          aria-label={`更换模型 · ${selectedModel?.name ?? '选择模型'}`}
          aria-expanded={focusSection === 'model'}
          className="agent-model-picker__current"
          onClick={() => setFocusSection(focusSection === 'model' && levels.length ? 'thinking' : 'model')}
          type="button"
        >
          <ProviderMark providerId={selection?.provider} size={20} />
          <span><strong>{selectedModel?.name ?? '选择模型'}</strong><small>{providerName}</small></span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
        {focusSection === 'model' ? <>

        <label className="agent-model-picker__search">
          <Search aria-hidden="true" size={15} />
          <Input
            aria-label="搜索模型"
            autoComplete="off"
            onChange={(event) => setQuery(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key !== 'ArrowDown') return;
              event.preventDefault();
              const selected = listRef.current?.querySelector<HTMLButtonElement>(
                '[role="option"][aria-selected="true"]',
              );
              const first = listRef.current?.querySelector<HTMLButtonElement>('[role="option"]');
              (selected ?? first)?.focus();
            }}
            placeholder="搜索模型"
            ref={searchRef}
            type="search"
            value={query}
          />
        </label>
        <ModelChoiceList
          ariaLabel="可用模型"
          groups={filteredGroups}
          listRef={listRef}
          onChoose={(option) => {
            setOpen(false);
            if (option.key === selectedKey) return;
            const model = options?.models.find((item) => item.reference === option.key) ?? catalog?.providers
              .find((item) => item.id === option.providerId)
              ?.models.find((item) => item.id === option.modelId);
            onChange(
              option.providerId,
              option.modelId,
              preferredThinkingLevel((model?.thinkingLevels ?? []) as ThinkingLevel[], thinking),
            );
          }}
          selectedKey={selectedKey}
          emptyLabel={query ? '没有找到匹配的模型' : '当前没有可用模型'}
        />
        </> : null}
        {levels.length > 0 ? (
          <div className="agent-model-picker__thinking">
            <p className="agent-thinking-picker__heading">
              <span>推理强度</span>
              <strong>{thinkingLabel(thinking)}</strong>
            </p>
            <ReasoningRail
              levels={levels}
              onChoose={(level) => {
                setOpen(false);
                if (!selection || level === thinking) return;
                onChange(selection.provider, selection.modelId, level);
              }}
              railRef={reasoningRef}
              selected={thinking}
            />
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}

function ReasoningRail({
  levels,
  railRef,
  selected,
  onChoose,
}: {
  levels: ThinkingLevel[];
  railRef: RefObject<HTMLDivElement | null>;
  selected: ThinkingLevel;
  onChoose: (level: ThinkingLevel) => void;
}) {
  const selectedIndex = Math.max(0, levels.indexOf(selected));
  const style = {
    '--level-count': Math.max(1, levels.length),
    '--level-index': selectedIndex,
  } as ReasoningRailStyle;
  return (
    <div
      aria-label="推理强度"
      className="agent-model-picker__reasoning"
      onKeyDown={(event) => moveButtonFocus(event, '[role="radio"]')}
      ref={railRef}
      role="radiogroup"
      style={style}
    >
      <span aria-hidden="true" className="agent-model-picker__reasoning-indicator" />
      {levels.map((level) => (
        <button
          aria-checked={level === selected}
          aria-label={thinkingLabel(level)}
          key={level}
          onClick={() => onChoose(level)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            onChoose(level);
          }}
          title={thinkingLabel(level)}
          role="radio"
          tabIndex={level === selected ? 0 : -1}
          type="button"
        >
          <span aria-hidden="true" className="agent-model-picker__reasoning-dot" />
        </button>
      ))}
    </div>
  );
}

function thinkingLabel(value: string): string {
  return ({
    off: '不启用推理',
    minimal: '轻量',
    low: '低',
    medium: '中',
    high: '高',
    xhigh: '极高',
    max: '最高',
  } as Record<string, string>)[value] ?? value;
}

function compactThinkingLabel(value: ThinkingLevel): string {
  return value === 'off' ? '关' : thinkingLabel(value);
}

function preferredThinkingLevel(
  levels: ThinkingLevel[],
  current: ThinkingLevel,
): ThinkingLevel {
  if (levels.includes(current)) return current;
  if (levels.includes('medium')) return 'medium';
  if (levels.includes('off')) return 'off';
  return levels[0] ?? 'off';
}

interface ReasoningRailStyle extends CSSProperties {
  '--level-count': number;
  '--level-index': number;
}
