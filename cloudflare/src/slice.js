const maxSliceBytes = 64 * 1024;
const decoder = new TextDecoder("utf-8", { fatal: true });
const isSpace = byte => byte === 32 || byte === 9 || byte === 10 || byte === 13;

function skipSpace(bytes, offset) {
  const limit = Math.min(bytes.length, offset + maxSliceBytes);
  while (offset < limit && isSpace(bytes[offset])) offset += 1;
  return offset;
}

function objectEnd(bytes, offset, limit) {
  let depth = 0;
  let quoted = false;
  for (; offset < limit; offset += 1) {
    const byte = bytes[offset];
    if (quoted) {
      if (byte === 92) offset += 1;
      else if (byte === 34) quoted = false;
    } else if (byte === 34) quoted = true;
    else if (byte === 123) depth += 1;
    else if (byte === 125 && --depth === 0) return offset + 1;
  }
  return null;
}

export function readJsonSlice(bytes, offset) {
  let start = offset;
  if (start === 0) {
    start = bytes[0] === 239 && bytes[1] === 187 && bytes[2] === 191 ? 3 : 0;
    start = skipSpace(bytes, start);
    if (bytes[start] !== 91) throw new Error("O INMET não retornou uma lista JSON.");
    start = skipSpace(bytes, start + 1);
  }
  const limit = Math.min(bytes.length, start + maxSliceBytes);
  let cursor = start;
  let end = start;
  for (;;) {
    if (bytes[cursor] !== 123) throw new Error("Observação inválida na lista do INMET.");
    const endOfObject = objectEnd(bytes, cursor, limit);
    if (endOfObject === null) break;
    end = endOfObject;
    cursor = skipSpace(bytes, end);
    if (bytes[cursor] === 93 && skipSpace(bytes, cursor + 1) === bytes.length) {
      cursor = null;
      break;
    }
    if (bytes[cursor] !== 44) throw new Error("Separador inválido na lista do INMET.");
    cursor = skipSpace(bytes, cursor + 1);
    if (cursor >= limit) break;
  }
  if (end === start) throw new Error("Observação truncada ou acima de 64 KiB.");
  return { text: "[" + decoder.decode(bytes.subarray(start, end)) + "]", next: cursor };
}
