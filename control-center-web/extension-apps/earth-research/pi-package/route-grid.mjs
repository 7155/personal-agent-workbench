/** Deterministic client-side path recovery over Earth Engine sampled costs.
 * Cells with missing/nonpositive costs are forbidden. Four-neighbor edges avoid
 * diagonal corner cutting. Validate the resulting geographic corridor in EE. */
export function routeGrid(costs, start, end, { clearanceCells = 0 } = {}) {
  const height = costs.length, width = costs[0]?.length;
  if (!height || !width || height * width > 262144 || !costs.every(row => Array.isArray(row) && row.length === width)) throw new Error('Expected a rectangular cost grid with at most 262144 cells');
  if (!Number.isInteger(clearanceCells) || clearanceCells < 0 || clearanceCells > 20) throw new Error('Invalid clearanceCells');
  const inside = ([row, col]) => Number.isInteger(row) && Number.isInteger(col) && row >= 0 && col >= 0 && row < height && col < width;
  if (!inside(start) || !inside(end)) return { status: 'invalid_endpoint', cells: [] };
  const valid = (row, col) => row >= 0 && col >= 0 && row < height && col < width && Number.isFinite(costs[row][col]) && costs[row][col] > 0;
  const allowed = (row, col) => {
    for (let dy = -clearanceCells; dy <= clearanceCells; dy++) for (let dx = -clearanceCells; dx <= clearanceCells; dx++) {
      if (dx * dx + dy * dy <= clearanceCells * clearanceCells && !valid(row + dy, col + dx)) return false;
    }
    return true;
  };
  const passable = Array.from({ length: height }, (_, row) => Array.from({ length: width }, (_, col) => allowed(row, col)));
  if (!passable[start[0]][start[1]] || !passable[end[0]][end[1]]) return { status: 'invalid_endpoint', cells: [] };
  const index = ([row, col]) => row * width + col;
  const from = index(start), to = index(end);
  const distances = new Float64Array(height * width).fill(Infinity);
  const previous = new Int32Array(height * width).fill(-1);
  const heap = [];
  const push = entry => { heap.push(entry); let i = heap.length - 1; while (i > 0) { const p = (i - 1) >> 1; if (heap[p][0] <= entry[0]) break; heap[i] = heap[p]; i = p; } heap[i] = entry; };
  const pop = () => {
    const first = heap[0], last = heap.pop(); if (!heap.length) return first;
    let i = 0;
    while (i * 2 + 1 < heap.length) { let c = i * 2 + 1; if (c + 1 < heap.length && heap[c + 1][0] < heap[c][0]) c++; if (heap[c][0] >= last[0]) break; heap[i] = heap[c]; i = c; }
    heap[i] = last; return first;
  };
  distances[from] = 0; push([0, from]);
  while (heap.length) {
    const [distance, current] = pop(); if (distance !== distances[current]) continue;
    if (current === to) break;
    const row = Math.floor(current / width), col = current % width;
    for (const [dy, dx] of [[-1, 0], [0, 1], [1, 0], [0, -1]]) {
      const r = row + dy, c = col + dx;
      if (!inside([r, c]) || !passable[r][c]) continue;
      const next = r * width + c, candidate = distance + (costs[row][col] + costs[r][c]) / 2;
      if (candidate < distances[next]) { distances[next] = candidate; previous[next] = current; push([candidate, next]); }
    }
  }
  if (!Number.isFinite(distances[to])) return { status: 'no_route', cells: [] };
  const cells = []; let current = to;
  while (current !== -1) { cells.push([Math.floor(current / width), current % width]); if (current === from) break; current = previous[current]; }
  cells.reverse(); return { status: 'completed', cells, gridCost: distances[to] };
}
