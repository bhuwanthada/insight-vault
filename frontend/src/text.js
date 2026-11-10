// Pure helpers that map between rendered text and character offsets in the stored note.

// Split text into plain and highlighted segments. Overlapping highlights are skipped.
export function buildSegments(text, highlights) {
  const sorted = [...highlights].sort((a, b) => a.start - b.start);
  const segments = [];
  let pos = 0;
  for (const h of sorted) {
    if (h.start < pos) continue;
    if (h.start > pos) segments.push({ start: pos, text: text.slice(pos, h.start) });
    segments.push({ start: h.start, text: text.slice(h.start, h.end), highlight: h });
    pos = h.end;
  }
  if (pos < text.length) segments.push({ start: pos, text: text.slice(pos) });
  return segments;
}

// Absolute character offset of (node, offset), found via the nearest data-off ancestor.
export function offsetOf(node, offset) {
  let el = node.nodeType === 3 ? node.parentElement : node;
  while (el && el.dataset.off === undefined) el = el.parentElement;
  return el ? Number(el.dataset.off) + offset : null;
}

// Convert a DOM Range into a trimmed { start, end } over the note text, or null.
export function selectionRange(range, text) {
  let start = offsetOf(range.startContainer, range.startOffset);
  let end = offsetOf(range.endContainer, range.endOffset);
  if (start === null || end === null) return null;
  if (end < start) [start, end] = [end, start];
  const raw = text.slice(start, end);
  start += raw.length - raw.trimStart().length;
  end -= raw.length - raw.trimEnd().length;
  return end > start ? { start, end } : null;
}
