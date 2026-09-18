import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const packageRoot = path.dirname(fileURLToPath(import.meta.url));
const knowledgePath = path.join(packageRoot, 'gis-knowledge.json');

function tokens(value) {
  return String(value || '').toLowerCase().match(/[a-z0-9_]+|[\u3400-\u9fff]{1,8}/g) || [];
}

function readKnowledge() {
  return JSON.parse(fs.readFileSync(knowledgePath, 'utf8'));
}

export function searchGISKnowledge(query, { limit = 8, category = '' } = {}) {
  const requested = tokens(query);
  if (!requested.length) return { query: String(query || ''), hits: [], total: 0, index: 'earth-gis-knowledge.v1' };
  const rows = readKnowledge().filter(row => !category || row.tags.includes(category));
  const scored = rows.map(row => {
    const haystack = tokens([row.title, row.text, ...row.tags].join(' '));
    const unique = new Set(haystack);
    const score = requested.reduce((sum, token) => sum + (unique.has(token) ? (row.tags.includes(token) ? 3 : 1) : 0), 0);
    return { ...row, score };
  }).filter(row => row.score > 0).sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
  return { query: String(query || ''), hits: scored.slice(0, Math.max(1, Math.min(20, limit))).map(({ score, ...row }) => ({ ...row, score })), total: scored.length, index: 'earth-gis-knowledge.v1' };
}

export function readGISKnowledge() { return readKnowledge(); }
