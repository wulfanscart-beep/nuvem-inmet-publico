import { batchKey, readBatch } from './batches.js';

import { defaultFields, fieldPositions, fields } from './fields.js';
import { bdmepKey, mirrorKey, packedKey, portalKey, readPackedStation, stationCode, wis2Key } from './packed.js';
const allFields = fields.map(({ name }) => name);

function response(body, type, status = 200, headers = {}) {
  return new Response(body, {
    status,
    headers: {
      'Content-Type': type,
      'Cache-Control': 'no-store',
      'Access-Control-Allow-Origin': '*',
      ...headers,
    },
  });
}

function json(body, status = 200, headers) {
  return response(JSON.stringify(body), 'application/json; charset=utf-8', status, headers);
}

function isUtcDay(day) {
  try {
    batchKey(day);
    return true;
  } catch {
    return false;
  }
}

function csvCell(value) {
  if (value === null) return '';
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function observationResponse(station, day, hours, format, selected, headers) {
  const rows = Array.from({ length: 24 }, (_, hour) => {
    const row = hours.get(hour);
    const values = {
      station_code: station,
      timestamp_utc: `${day}T${String(hour).padStart(2, '0')}:00:00Z`,
    };
    for (const name of selected) values[name] = row?.[fieldPositions.get(name)] ?? null;
    values.recorded = row !== undefined;
    return values;
  });
  if (format === 'csv') {
    const observationColumns = ['station_code', 'timestamp_utc', ...selected, 'recorded'];
    const lines = rows.map(row => observationColumns.map(column => csvCell(row[column])).join(','));
    return response(`${observationColumns.join(',')}\n${lines.join('\n')}\n`, 'text/csv; charset=utf-8', 200, headers);
  }
  return json(rows, 200, headers);
}

async function legacyHours(bucket, day, station) {
  const batch = await readBatch(bucket, day);
  if (batch === null) return null;
  const hours = new Map();
  for (const row of batch) {
    if (!Array.isArray(row) || row.length !== 4) throw new TypeError('Invalid stored observation');
    if (row[0] !== station) continue;
    const [, hour, temperature, humidity] = row;
    if (!Number.isInteger(hour) || hour < 0 || hour > 23 || hours.has(hour)
        || (temperature !== null && typeof temperature !== 'string')
        || (humidity !== null && typeof humidity !== 'string')) {
      throw new TypeError('Invalid stored observation');
    }
    hours.set(hour, row.slice(1));
  }
  return hours;
}

async function storedHours(env, day, station) {
  if (!env?.INMET_DAYS) return legacyHours(env?.INMET_BATCHES, day, station);
  const bytes = await env.INMET_DAYS.get(packedKey(day), 'arrayBuffer');
  return bytes === null ? null : readPackedStation(bytes, day, station);
}

function coverageHeaders(source, hours, coverage = 'partial') {
  return {
    'X-INMET-Source': source,
    'X-INMET-Coverage': coverage,
    'X-INMET-Observed-Hours': String(hours.size),
    'Access-Control-Expose-Headers': 'X-INMET-Source, X-INMET-Coverage, X-INMET-Observed-Hours',
  };
}

function missingValues(hours, selected) {
  return !hours || hours.size < 24 || [...hours.values()].some(row =>
    selected.some(name => row[fieldPositions.get(name)] === null));
}

function fillMissing(hours, reports) {
  if (!hours?.size) return reports;
  for (const [hour, row] of reports) {
    const primary = hours.get(hour);
    hours.set(hour, primary ? primary.map((value, index) => value ?? row[index]) : row);
  }
  return hours;
}

function addsReadings(hours, reports, selected) {
  return [...reports].some(([hour, row]) => {
    const known = hours?.get(hour);
    return !known || selected.some(name => {
      const column = fieldPositions.get(name);
      return known[column] == null && row[column] != null;
    });
  });
}

export async function handleRequest(request, env) {
  const url = new URL(request.url);
  if (!['/health', '/v1/observations', '/v1/fields'].includes(url.pathname)) {
    return json({ error: 'route_not_found' }, 404);
  }
  if (request.method !== 'GET') {
    return json({ error: 'method_not_allowed' }, 405, { Allow: 'GET' });
  }

  if (url.pathname === '/v1/fields') return json(fields);

  if (url.pathname === '/health') {
    try {
      if (env?.INMET_DAYS) await env.INMET_DAYS.get('health');
      else await env?.INMET_BATCHES.head(batchKey(new Date().toISOString().slice(0, 10)));
      if (!env?.INMET_DAYS && !env?.INMET_BATCHES) throw new Error('Missing storage binding');
      return json({ status: 'ok' });
    } catch {
      return json({ error: 'storage_unavailable' }, 503);
    }
  }

  const stations = url.searchParams.getAll('station');
  const dates = url.searchParams.getAll('date');
  const formats = url.searchParams.getAll('format');
  const station = stations[0];
  const day = dates[0];
  const format = formats[0] ?? 'json';
  if (stations.length !== 1 || station !== station.trim() || !stationCode.test(station)) {
    return json({ error: 'invalid_station' }, 400);
  }
  if (dates.length !== 1 || !isUtcDay(day)) {
    return json({ error: 'invalid_date' }, 400);
  }
  if (formats.length > 1 || (format !== 'json' && format !== 'csv')) {
    return json({ error: 'invalid_format' }, 400);
  }

  const selections = url.searchParams.getAll('fields');
  const requested = selections[0];
  const selected = requested === 'all' ? allFields : requested === undefined ? defaultFields : requested.split(',');
  if (selections.length > 1 || selected.length > fields.length
      || new Set(selected).size !== selected.length || selected.some(name => !fieldPositions.has(name))) {
    return json({ error: 'invalid_fields' }, 400);
  }

  let hours;
  let headers;
  let officialDayExists = false;
  try {
    hours = await storedHours(env, day, station);
    if (!hours?.size && env?.INMET_DAYS) {
      const portal = await env.INMET_DAYS.get(portalKey(day, station), 'arrayBuffer');
      if (portal !== null) {
        hours = await readPackedStation(portal, day, station);
        headers = coverageHeaders('portal', hours, hours.size === 24 ? 'complete' : 'partial');
      }
    }
    const fromPortal = headers?.['X-INMET-Source'] === 'portal';
    const portalHasGaps = fromPortal && missingValues(hours, selected);
    if (env?.INMET_DAYS && (!hours?.size || portalHasGaps)) {
      const bdmep = await env.INMET_DAYS.get(bdmepKey(day), 'arrayBuffer');
      if (bdmep !== null) {
        officialDayExists = true;
        const reports = await readPackedStation(bdmep, day, station);
        if (reports.size > 0) {
          hours = fillMissing(hours, reports);
          headers = coverageHeaders(fromPortal ? 'portal' : 'bdmep', hours, hours.size === 24 ? 'complete' : 'partial');
        }
      }
    }
    const from = headers?.['X-INMET-Source'];
    if (env?.INMET_DAYS && (!hours?.size || (from && missingValues(hours, selected)))) {
      const mirror = await env.INMET_DAYS.get(mirrorKey(day), 'arrayBuffer');
      if (mirror !== null) {
        officialDayExists = true;
        const reports = await readPackedStation(mirror, day, station);
        if (addsReadings(hours, reports, selected)) {
          hours = fillMissing(hours, reports);
          headers = coverageHeaders(from ?? 'agencia-sertao', hours, hours.size === 24 ? 'complete' : 'partial');
          if (from) {
            headers['X-INMET-Supplement'] = 'agencia-sertao';
            headers['Access-Control-Expose-Headers'] += ', X-INMET-Supplement';
          }
        }
      }
    }
    if (env?.INMET_DAYS && url.searchParams.get('fallback') === 'wis2' && missingValues(hours, selected)) {
      const fallback = await env.INMET_DAYS.get(wis2Key(day), 'arrayBuffer');
      if (fallback !== null) {
        officialDayExists = true;
        const reports = await readPackedStation(fallback, day, station);
        if (addsReadings(hours, reports, selected)) {
          const hasPrimary = Boolean(hours?.size);
          hours = fillMissing(hours, reports);
          if (hasPrimary) {
            const priorSupplement = headers?.['X-INMET-Supplement'];
            headers = coverageHeaders(headers?.['X-INMET-Source'] ?? 'inmet', hours, hours.size === 24 ? 'complete' : 'partial');
            headers['X-INMET-Supplement'] = priorSupplement ? priorSupplement + ', wis2' : 'wis2';
            headers['Access-Control-Expose-Headers'] += ', X-INMET-Supplement';
          } else {
            headers = coverageHeaders('wis2', hours);
          }
        }
      }
    }
  } catch {
    return json({ error: 'storage_unavailable' }, 503);
  }
  if (hours === null) return json({ error: officialDayExists ? 'station_not_found' : 'day_not_found' }, 404);
  if (!hours.size) return json({ error: 'station_not_found' }, 404);
  return observationResponse(station, day, hours, format, selected, headers);
}
