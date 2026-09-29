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

  it('url= is the encoded tos URL with a trailing slash; a private TOS dataset appends its region and id (design doc 15)', () => {
    const d = { id: 'ds-kqzmrtbwe', source: 'tos' as const, uri: 'tos://pai-kit-datasets/lerobot/droid_100', region: 'cn-beijing' };
    const encoded = 'tos%3A%2F%2Fpai-kit-datasets%2Flerobot%2Fdroid_100%2F%3Fregion%3Dcn-beijing%26curator_dataset%3Dds-kqzmrtbwe';
    expect(rerunViewerUrl(d, '/curation', ORIGIN)).toBe(`${ORIGIN}/?url=${encoded}`);
    expect(rerunViewerUrl(d, '', ORIGIN)).toBe(`${ORIGIN}/?url=${encoded}`);
    expect(rerunViewerUrl(d, '/a/b', ORIGIN)).toBe(`${ORIGIN}/a/?url=${encoded}`);
    // The value decodes back to what ReRun parses: the id sits next to region in the tos URL's own query.
    const url = new URL(rerunViewerUrl(d, '/curation', ORIGIN)!);
    expect(url.searchParams.get('url')).toBe('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing&curator_dataset=ds-kqzmrtbwe');
    expect([...url.searchParams.keys()]).toEqual(['url']);
  });

  it('a private TOS dataset without a region carries only the id; older ids work the same', () => {
    const d = { id: 'ds_01HXR2D8QZ', source: 'tos' as const, uri: 'tos://b/p/name/', region: null };
    expect(new URL(rerunViewerUrl(d, '/curation', ORIGIN)!).searchParams.get('url')).toBe('tos://b/p/name/?curator_dataset=ds_01HXR2D8QZ');
  });

  it('a public dataset carries no id: the viewer reads the cache bucket anonymously, as before', () => {
    expect(new URL(rerunViewerUrl({ id: 'ds-publicaaa', source: 'public', uri: 'tos://b/p/name/', region: 'cn-shanghai' }, '/curation', ORIGIN)!).searchParams.get('url')).toBe('tos://b/p/name/?region=cn-shanghai');
    expect(new URL(rerunViewerUrl({ id: 'ds-liberoaaa', source: 'public', uri: 'tos://hf-cache/lerobot/libero_10', region: null }, '/curation', ORIGIN)!).searchParams.get('url')).toBe('tos://hf-cache/lerobot/libero_10/');
  });

  it('a locally mounted dataset cannot be visualized', () => {
    expect(canVisualize({ uri: '/mnt/datasets/droid_100' })).toBe(false);
    expect(canVisualize({ uri: 'tos://bucket-only' })).toBe(false);
    expect(rerunViewerUrl({ id: 'ds-localaaaa', source: 'local', uri: '/mnt/datasets/droid_100', region: null }, '/curation', ORIGIN)).toBeNull();
  });
});
