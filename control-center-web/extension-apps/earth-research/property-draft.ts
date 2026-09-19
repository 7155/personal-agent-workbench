/**
 * The property editor is a text input, but the persisted feature properties
 * are typed JSON values. Keep the original value beside the input so a draft
 * that was never edited can be written back without guessing its type.
 */
export type PropertyDraftEntry = {
  original: unknown;
  value: string;
  edited: boolean;
};

export type PropertyDraft = Record<string, PropertyDraftEntry>;

export type MaterializedPropertyDraft =
  | { ok: true; properties: Record<string, unknown> }
  | { ok: false; error: string };

function displayValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined) return '';
  const serialized = JSON.stringify(value);
  return serialized === undefined ? String(value) : serialized;
}

export function createPropertyDraft(properties: Record<string, unknown> | null | undefined): PropertyDraft {
  return Object.fromEntries(Object.entries(properties ?? {}).map(([key, original]) => [key, {
    original,
    value: displayValue(original),
    edited: false,
  }]));
}

export function updatePropertyDraft(draft: PropertyDraft, key: string, value: string): PropertyDraft {
  const current = draft[key];
  if (!current) return draft;
  return { ...draft, [key]: { ...current, value, edited: value !== displayValue(current.original) } };
}

export function discardPropertyDraft(draft: PropertyDraft): PropertyDraft {
  return Object.fromEntries(Object.entries(draft).map(([key, entry]) => [key, {
    ...entry,
    value: displayValue(entry.original),
    edited: false,
  }]));
}

function parseEditedValue(key: string, entry: PropertyDraftEntry): unknown {
  // A string remains a string even when it happens to contain valid JSON.
  if (typeof entry.original === 'string') return entry.value;

  if (typeof entry.original === 'number') {
    const value = entry.value.trim();
    if (!value) throw new Error(`属性“${key}”需要数字。`);
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error(`属性“${key}”不是有效数字。`);
    return parsed;
  }

  if (typeof entry.original === 'boolean') {
    const value = entry.value.trim().toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    throw new Error(`属性“${key}”需要 true 或 false。`);
  }

  if (entry.original !== null && typeof entry.original === 'object') {
    let parsed: unknown;
    try {
      parsed = JSON.parse(entry.value);
    } catch {
      throw new Error(`属性“${key}”需要有效 JSON。`);
    }
    if (parsed === null || typeof parsed !== 'object') throw new Error(`属性“${key}”需要对象或数组。`);
    if (Array.isArray(entry.original) !== Array.isArray(parsed)) throw new Error(`属性“${key}”的数组/对象类型不能改变。`);
    return parsed;
  }

  // Null and undefined are preserved while untouched. Once a user edits the
  // field, the text is intentional input and is kept as text because null is
  // not a signal to run a JSON parser.
  return entry.value;
}

export function materializePropertyDraft(draft: PropertyDraft): MaterializedPropertyDraft {
  try {
    const properties = Object.fromEntries(Object.entries(draft).map(([key, entry]) => [
      key,
      entry.edited ? parseEditedValue(key, entry) : entry.original,
    ]));
    return { ok: true, properties };
  } catch (reason) {
    return { ok: false, error: reason instanceof Error ? reason.message : String(reason) };
  }
}

export function hasEditedProperties(draft: PropertyDraft): boolean {
  return Object.values(draft).some(entry => entry.edited);
}
