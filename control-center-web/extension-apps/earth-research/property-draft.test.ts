import { describe, expect, it } from 'vitest';
import { createPropertyDraft, discardPropertyDraft, hasEditedProperties, materializePropertyDraft, updatePropertyDraft } from './property-draft';

describe('property draft typing', () => {
  it('preserves untouched values, including null and nested objects', () => {
    const original = { label: '001', count: 3, enabled: false, metadata: { source: 'local' }, missing: null };
    const result = materializePropertyDraft(createPropertyDraft(original));
    expect(result).toEqual({ ok: true, properties: original });
  });

  it('keeps strings as strings while parsing edited primitive and object fields by original type', () => {
    let draft = createPropertyDraft({ label: 'false', count: 3, enabled: false, metadata: { source: 'local' } });
    draft = updatePropertyDraft(draft, 'label', 'true');
    draft = updatePropertyDraft(draft, 'count', '4.5');
    draft = updatePropertyDraft(draft, 'enabled', 'true');
    draft = updatePropertyDraft(draft, 'metadata', '{"source":"remote"}');
    expect(materializePropertyDraft(draft)).toEqual({ ok: true, properties: { label: 'true', count: 4.5, enabled: true, metadata: { source: 'remote' } } });
  });

  it('rejects invalid typed edits instead of silently changing the property type', () => {
    const draft = updatePropertyDraft(createPropertyDraft({ count: 3, enabled: false, metadata: {} }), 'count', 'not-a-number');
    expect(materializePropertyDraft(draft)).toEqual({ ok: false, error: '属性“count”不是有效数字。' });
  });

  it('discards edits back to the original values', () => {
    const draft = updatePropertyDraft(createPropertyDraft({ name: 'before', count: 1 }), 'name', 'after');
    expect(hasEditedProperties(draft)).toBe(true);
    const discarded = discardPropertyDraft(draft);
    expect(hasEditedProperties(discarded)).toBe(false);
    expect(materializePropertyDraft(discarded)).toEqual({ ok: true, properties: { name: 'before', count: 1 } });
  });

  it('clears dirty state when the user restores the original text, including null', () => {
    let draft = updatePropertyDraft(createPropertyDraft({ code: '001', missing: null }), 'missing', 'something');
    expect(hasEditedProperties(draft)).toBe(true);
    draft = updatePropertyDraft(draft, 'missing', 'null');
    expect(hasEditedProperties(draft)).toBe(false);
    expect(materializePropertyDraft(draft)).toEqual({ ok: true, properties: { code: '001', missing: null } });
  });

  it('does not parse untouched JSON-like strings or stringify null when editing another field', () => {
    const original = { code: '001', truth: 'true', empty: 'null', json: '{"a":1}', nil: null, nested: [1, false], optional: undefined, name: 'before' };
    const draft = updatePropertyDraft(createPropertyDraft(original), 'name', 'after');
    expect(materializePropertyDraft(draft)).toEqual({ ok: true, properties: { ...original, name: 'after' } });
  });

  it('rejects blank or non-finite numeric edits and array/object type changes', () => {
    for (const input of ['', 'Infinity', 'NaN']) expect(materializePropertyDraft(updatePropertyDraft(createPropertyDraft({ count: 3 }), 'count', input)).ok).toBe(false);
    expect(materializePropertyDraft(updatePropertyDraft(createPropertyDraft({ metadata: [] }), 'metadata', '{}')).ok).toBe(false);
    expect(materializePropertyDraft(updatePropertyDraft(createPropertyDraft({ metadata: {} }), 'metadata', '[]')).ok).toBe(false);
  });
});
