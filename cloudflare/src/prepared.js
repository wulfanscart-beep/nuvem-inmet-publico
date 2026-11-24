const MAX_BATCH_BYTES = 2 * 1024 * 1024;

export async function fetchPreparedBatch(template, day, fetcher = fetch) {
  if (!template.includes("{date}")) {
    throw new Error("INMET_BATCH_URL precisa conter {date}.");
  }
  const url = new URL(template.replaceAll("{date}", day));
  if (url.protocol !== "https:" || url.username || url.password) {
    throw new Error("A URL do lote precisa usar HTTPS.");
  }

  const response = await fetcher(url, { redirect: "manual", signal: AbortSignal.timeout(30_000) });
  if (response.status !== 200) {
    throw new Error(`A fonte do lote retornou HTTP ${response.status} para ${day}.`);
  }
  const encoding = response.headers.get("content-encoding");
  if (!response.body || (encoding && encoding !== "identity")) {
    throw new Error("A fonte precisa servir o arquivo .gz como corpo, sem Content-Encoding.");
  }
  if (Number(response.headers.get("content-length")) > MAX_BATCH_BYTES) {
    await response.body.cancel();
    throw new Error("Lote diário acima de 2 MiB.");
  }

  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BATCH_BYTES) throw new Error("Lote diário acima de 2 MiB.");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  if (size < 18 || bytes[0] !== 0x1f || bytes[1] !== 0x8b || bytes[2] !== 8) {
    throw new Error("A fonte não entregou um arquivo gzip válido para o lote.");
  }
  return bytes.buffer;
}
