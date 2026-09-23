import { describe, expect, it } from 'vitest';
import { canVisualize, parentBase, rerunViewerUrl } from './rerun';

const ORIGIN = 'https://dataverse.example.com';

describe('「可视化」 opens the ReRun viewer of the same deployment (requester item 22)', () => {
  it('the viewer sits one level above the console prefix', () => {
    expect(parentBase('')).toBe('');
    expect(parentBase('/curation')).toBe('');
    expect(parentBase('/curation/')).toBe('');
    expect(parentBase('/a/b')).toBe('/a');
    expect(parentBase('a/b/')).toBe('/a');
  });

  it('url= is the encoded tos URL with a trailing slash, the region appended to it', () => {
    const d = { uri: 'tos://pai-kit-datasets/lerobot/droid_100', region: 'cn-beijing' };
    const encoded = 'tos%3A%2F%2Fpai-kit-datasets%2Flerobot%2Fdroid_100%2F%3Fregion%3Dcn-beijing';
    expect(rerunViewerUrl(d, '/curation', ORIGIN)).toBe(`${ORIGIN}/?url=${encoded}`);
    expect(rerunViewerUrl(d, '', ORIGIN)).toBe(`${ORIGIN}/?url=${encoded}`);
    expect(rerunViewerUrl(d, '/a/b', ORIGIN)).toBe(`${ORIGIN}/a/?url=${encoded}`);
    // The value decodes back to what ReRun parses.
    const url = new URL(rerunViewerUrl(d, '/curation', ORIGIN)!);
    expect(url.searchParams.get('url')).toBe('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing');
  });

  it('keeps one trailing slash; no region, no suffix (a public dataset)', () => {
    expect(new URL(rerunViewerUrl({ uri: 'tos://b/p/name/', region: 'cn-shanghai' }, '/curation', ORIGIN)!).searchParams.get('url')).toBe('tos://b/p/name/?region=cn-shanghai');
    expect(new URL(rerunViewerUrl({ uri: 'tos://hf-cache/lerobot/libero_10', region: null }, '/curation', ORIGIN)!).searchParams.get('url')).toBe('tos://hf-cache/lerobot/libero_10/');
  });

  it('a locally mounted dataset cannot be visualized', () => {
    expect(canVisualize({ uri: '/mnt/datasets/droid_100' })).toBe(false);
    expect(canVisualize({ uri: 'tos://bucket-only' })).toBe(false);
    expect(rerunViewerUrl({ uri: '/mnt/datasets/droid_100' }, '/curation', ORIGIN)).toBeNull();
  });
});
