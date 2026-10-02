import { describe, expect, it } from 'vitest';
import { parseInvidiousSearch, parsePipedSearch, youtubeIdFromPipedUrl } from './search';

describe('search parsers', () => {
  it('reads a Piped search payload into tracks', () => {
    const tracks = parsePipedSearch({
      items: [
        {
          url: '/watch?v=dQw4w9WgXcQ',
          type: 'stream',
          title: 'Sample',
          uploaderName: 'Channel',
          duration: 212,
        },
        { url: '/playlist?list=PL123', title: 'not a video' },
        {
          url: '/watch?v=dQw4w9WgXcQ&t=3',
          title: 'duplicate',
          uploaderName: 'Channel',
          duration: 212,
        },
      ],
    });
    expect(tracks).toEqual([
      {
        videoId: 'dQw4w9WgXcQ',
        title: 'Sample',
        channel: 'Channel',
        durationSec: 212,
        thumbnail: 'https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg',
      },
    ]);
  });

  it('rejects a Piped payload without items', () => {
    expect(() => parsePipedSearch({ error: 'nope' })).toThrow(/piped/);
  });

  it('reads an Invidious search payload', () => {
    const tracks = parseInvidiousSearch([
      {
        type: 'video',
        title: 'Live set',
        videoId: 'abcdefghijk',
        author: 'DJ',
        lengthSeconds: -1,
      },
      { type: 'playlist', title: 'mix', videoId: 'aaaaaaaaaaa' },
    ]);
    expect(tracks).toHaveLength(1);
    expect(tracks[0]?.durationSec).toBe(-1);
    expect(tracks[0]?.channel).toBe('DJ');
  });

  it('rejects an Invidious object payload', () => {
    expect(() => parseInvidiousSearch({ error: 'down' })).toThrow(/invidious/);
  });

  it('pulls an 11-character id out of a watch url', () => {
    expect(youtubeIdFromPipedUrl('/watch?v=dQw4w9WgXcQ')).toBe('dQw4w9WgXcQ');
    expect(youtubeIdFromPipedUrl('/watch?v=short')).toBeNull();
  });
});
