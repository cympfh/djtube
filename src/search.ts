export type Track = {
  videoId: string;
  title: string;
  channel: string;
  /** Seconds, `null` when unknown, negative for a livestream. */
  durationSec: number | null;
  thumbnail: string;
};

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const RESULT_LIMIT = 15;

/** An instance answered, and the query really has no videos. */
class EmptySearch extends Error {
  constructor() {
    super('empty');
    this.name = 'EmptySearch';
  }
}

/**
 * Public YouTube-frontend instances. A static page cannot call the YouTube
 * Data API without embedding a key, so search goes through these CORS-enabled
 * endpoints and playback still uses the official IFrame Player API.
 * Instances rot; both lists are tried, and the first success wins.
 */
const PIPED_BASES = ['https://pipedapi.ducks.party', 'https://api.piped.private.coffee'];
const INVIDIOUS_BASES = ['https://invidious.f5.si'];

export function youtubeIdFromPipedUrl(url: string): string | null {
  const match = /[?&]v=([A-Za-z0-9_-]{11})/.exec(url);
  if (!match || !VIDEO_ID.test(match[1])) return null;
  return match[1];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object') return null;
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function thumbnailFor(videoId: string): string {
  return `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg`;
}

function pushTrack(tracks: Track[], track: Track | null): void {
  if (!track) return;
  if (tracks.some((item) => item.videoId === track.videoId)) return;
  tracks.push(track);
}

export function parsePipedSearch(data: unknown): Track[] {
  const record = asRecord(data);
  if (!record || !Array.isArray(record.items)) {
    throw new Error('unexpected piped payload');
  }
  const tracks: Track[] = [];
  for (const item of record.items) {
    const row = asRecord(item);
    if (!row) continue;
    const videoId = youtubeIdFromPipedUrl(text(row.url));
    if (!videoId) continue;
    const duration = finiteNumber(row.duration);
    pushTrack(tracks, {
      videoId,
      title: text(row.title) || videoId,
      channel: text(row.uploaderName) || text(row.uploader) || '',
      durationSec: duration,
      thumbnail: thumbnailFor(videoId),
    });
    if (tracks.length >= RESULT_LIMIT) break;
  }
  return tracks;
}

export function parseInvidiousSearch(data: unknown): Track[] {
  if (!Array.isArray(data)) throw new Error('unexpected invidious payload');
  const tracks: Track[] = [];
  for (const item of data) {
    const row = asRecord(item);
    if (!row) continue;
    const kind = text(row.type);
    if (kind && kind !== 'video' && kind !== 'shortVideo') continue;
    const videoId = text(row.videoId);
    if (!VIDEO_ID.test(videoId)) continue;
    const duration = finiteNumber(row.lengthSeconds);
    pushTrack(tracks, {
      videoId,
      title: text(row.title) || videoId,
      channel: text(row.author) || '',
      durationSec: duration,
      thumbnail: thumbnailFor(videoId),
    });
    if (tracks.length >= RESULT_LIMIT) break;
  }
  return tracks;
}

function withTimeout(parent: AbortSignal | undefined, ms: number): AbortSignal {
  const timeout = AbortSignal.timeout(ms);
  if (!parent) return timeout;
  if (typeof AbortSignal.any === 'function') return AbortSignal.any([parent, timeout]);
  const controller = new AbortController();
  const abort = () => controller.abort();
  parent.addEventListener('abort', abort, { once: true });
  timeout.addEventListener('abort', abort, { once: true });
  return controller.signal;
}

async function fetchJson(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(url, {
    signal,
    mode: 'cors',
    credentials: 'omit',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<unknown>;
}

async function searchPiped(base: string, query: string, signal: AbortSignal): Promise<Track[]> {
  const url = `${base}/search?q=${encodeURIComponent(query)}&filter=videos`;
  return parsePipedSearch(await fetchJson(url, signal));
}

async function searchInvidious(base: string, query: string, signal: AbortSignal): Promise<Track[]> {
  const url = `${base}/api/v1/search?q=${encodeURIComponent(query)}&type=video`;
  return parseInvidiousSearch(await fetchJson(url, signal));
}

export async function searchTracks(query: string, signal?: AbortSignal): Promise<Track[]> {
  const q = query.trim();
  if (!q) return [];
  const jobs = [
    ...PIPED_BASES.map((base) => searchPiped(base, q, withTimeout(signal, 8000))),
    ...INVIDIOUS_BASES.map((base) => searchInvidious(base, q, withTimeout(signal, 8000))),
  ].map((job) =>
    job.then((tracks) => {
      if (tracks.length === 0) throw new EmptySearch();
      return tracks;
    }),
  );
  try {
    return await Promise.any(jobs);
  } catch (error) {
    // A fast empty reply must not hide a slower instance that has hits.
    // If somebody did answer with an empty list, the query itself is empty.
    if (error instanceof AggregateError && error.errors.some((item) => item instanceof EmptySearch)) {
      return [];
    }
    throw new Error('search failed');
  }
}
