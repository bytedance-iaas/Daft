// Port of v1's deep-link tests (release_v1 @ 45bdf9292):
//   backend/curation/tests/test_ui_datasources.py, test_tos_merge.py (split_dataset_url),
//   test_tos_probe.py (borrowed_output_url), test_public_catalog.py (public source rules).
//
// Every v1 case is listed here, in v1's order, with how it maps to v2:
//   test_missing_tos_buckets_synthesizes_single_bucket ........ n/a: site bucket config (no mounts in v2)
//   test_configured_buckets_keep_order_and_skip_broken_entries  n/a: site bucket config
//   test_bucket_path_rejects_forged_identifiers ................ n/a: mount whitelist; v2 never rewrites
//                                                                 an address (see «unknown bucket» below)
//   test_deeplink_values_reads_all_three_keys_and_merges ....... ported as is
//   test_parse_dataset_ref_is_deterministic_on_odd_inputs ...... ported as is
//   test_deeplink_bucket_match_preselects_source_and_dataset ... ported: match = a registered dataset
//   test_deeplink_unknown_bucket_never_falls_back_to_default ... ported (the decoy of the same name stays)
//   test_deeplink_known_bucket_missing_dataset_says_so ......... ported for bare names; a tos:// address
//                                                                 is checked by the automatic preflight
//   test_deeplink_prefix_mismatch_is_rejected_not_matched ...... ported (decoy under another prefix)
//   test_deeplink_unknown_local_prefix_never_admits_match ...... ported: non-TOS registrations never match
//   test_deeplink_bare_name_keeps_todays_behavior .............. ported: registry instead of default bucket
//   test_deeplink_never_silent_when_nothing_selected ........... ported as is
//   test_deeplink_refs_spanning_two_sources_abstain_with_notice  ported (URLs and bare names)
//   test_deeplink_real_listing_end_to_end ...................... covered end to end in the new-task page test
//   test_selected_source_root_reaches_input_argv ............... covered in the page test (request body)
//   test_declared_bucket_label_* / test_bucket_label_* /
//   test_synthesized_bucket_label_* / test_alias_only_* ........ n/a: labels of mounted buckets
//   test_deeplink_info_uses_display_label ...................... ported: success is silent in v2 (as v1 since
//                                                                 2026-08-29: «字段填上就是最好的证明»)
//   test_old_config_key_* / test_prefix_derivation_* /
//   test_bucket_info_line_* / test_probe_dataset_root_* ........ n/a: site config and mounts
//   test_deeplink_without_endpoint_four_wordings_verbatim ...... ported for the wordings that still exist
//   test_deeplink_endpoint_same_region_cross_network_domain_is_silent  ported
//   test_deeplink_endpoint_region_conflict_names_both_regions_keeps_selection  ported
//   test_deeplink_endpoint_appended_to_unmatched_bucket_notice . ported (unmatched name notice)
//   test_deeplink_endpoint_two_key_names_both_recognized ....... ported as is
//   test_endpoint_sanitizer_strips_or_rejects_untrusted_input .. ported as is (+ pipeline level)
//   test_endpoint_region_unrecognized_skips_conflict_and_never_crashes  ported
//   test_tos_merge: test_split_dataset_url_* (3) ............... ported as is
//   test_tos_probe: test_borrowed_output_url ................... ported: «own bucket» = last delivery root
//   test_public_catalog: test_runner_never_borrows_public_bucket_and_public_output_default  ported
//   retired UI test test_public_deeplink_preselects_and_switches_source  ported (planDeepLink source=public)
import { describe, expect, it } from 'vitest';
import {
  borrowedOutputUrl,
  deeplinkEndpoint,
  deeplinkRegion,
  deeplinkValues,
  endpointRegion,
  parseDatasetRef,
  planDeepLink,
  publicOutputDefault,
  sanitizeEndpoint,
  splitDatasetUrl,
  suggestDeliveryName,
  type PlanContext,
  type RegisteredDataset,
} from './deeplink';

const reg = (id: string, uri: string, region: string | null = 'cn-beijing', source: RegisteredDataset['source'] = 'tos'): RegisteredDataset => ({
  id,
  name: uri.replace(/\/+$/, '').split('/').pop() ?? id,
  source,
  uri,
  region,
});

const ctx = (registered: RegisteredDataset[], extra: Partial<PlanContext> = {}): PlanContext => ({
  registered,
  publicDatasets: null,
  homeRegion: null,
  ...extra,
});

const q = (s: string) => new URLSearchParams(s);

describe('deeplinkValues (test_deeplink_values_reads_all_three_keys_and_merges)', () => {
  it('reads all three keys, merges in key order and de-duplicates', () => {
    expect(deeplinkValues({ dataset: 'a,b' })).toEqual({ values: ['a', 'b'], present: true });
    expect(deeplinkValues({ dataset_url: 'tos://x/datasets/c' })).toEqual({ values: ['tos://x/datasets/c'], present: true });
    expect(deeplinkValues({ url: 'tos://x/d' })).toEqual({ values: ['tos://x/d'], present: true });
    // dataset → dataset_url → url, duplicates kept once
    expect(deeplinkValues({ dataset: 'a', url: 'a,b', dataset_url: 'c' })).toEqual({ values: ['a', 'c', 'b'], present: true });
    // key present with an empty value: present must be true (the page has to say something)
    expect(deeplinkValues({ dataset: '' })).toEqual({ values: [], present: true });
    // unrelated keys: stay silent
    expect(deeplinkValues({ foo: 'bar' })).toEqual({ values: [], present: false });
    // URLSearchParams with repeated keys, like starlette's getlist
    expect(deeplinkValues(q('dataset=x&dataset=y,x&url=z'))).toEqual({ values: ['x', 'y', 'z'], present: true });
  });
});

describe('parseDatasetRef (test_parse_dataset_ref_is_deterministic_on_odd_inputs)', () => {
  it('parses bare names and tos:// URLs deterministically', () => {
    expect(parseDatasetRef('demo_v2')).toEqual({ bucket: null, prefix: null, dataset: 'demo_v2' });
    // trailing slash tolerated; bucket lowercased
    expect(parseDatasetRef('tos://BucketA/datasets/demo_v2/')).toEqual({ bucket: 'bucketa', prefix: 'datasets', dataset: 'demo_v2' });
    // prefix and name are case-sensitive
    const got = parseDatasetRef('tos://b/DS/Demo');
    expect([got.prefix, got.dataset]).toEqual(['DS', 'Demo']);
    // multi-level prefixes kept; a dataset at the bucket root has prefix '' (not null)
    expect(parseDatasetRef('tos://b/raw/2026/x').prefix).toBe('raw/2026');
    expect(parseDatasetRef('tos://b/x').prefix).toBe('');
  });

  it.each([
    'tos://bucketa', // bucket only
    'tos://', // nothing
    'tos:///datasets/x', // empty bucket
    'https://host/datasets/x', // not tos
    'tos://b/datasets/demo%20v2', // %20 → space, fails safe_name
    'tos://b/datasets/a%2Fb', // %2F → a hidden slash
  ])('refuses %s with an error and no dataset', (bad) => {
    const got = parseDatasetRef(bad);
    expect(got.error).toBeTruthy();
    expect(got.dataset).toBeUndefined();
  });
});

describe('planDeepLink: the three outcomes and compatibility', () => {
  it('test_deeplink_bucket_match_preselects_source_and_dataset: a registered address is selected with its registration', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2'), reg('ds_b', 'tos://bucketa/datasets/demo_v2'), reg('ds_x', 'tos://bucketa/datasets/x')];
    const plan = planDeepLink(q('dataset=tos://bucketA/datasets/demo_v2'), ctx(registered));
    expect(plan.source).toBe('tos');
    expect(plan.datasets).toEqual([{ uri: 'tos://bucketa/datasets/demo_v2', name: 'demo_v2', datasetId: 'ds_b', region: 'cn-beijing' }]);
    expect(plan.notes).toEqual({ dataset: [], region: [] });
  });

  it('test_deeplink_unknown_bucket_never_falls_back_to_default: the link bucket is kept, the decoy is not picked', () => {
    // A registered dataset of the same name in another bucket is the decoy.
    const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2'), reg('ds_b', 'tos://bucketa/datasets/demo_v2')];
    const plan = planDeepLink(q('dataset=tos://strange/datasets/demo_v2'), ctx(registered));
    expect(plan.datasets).toEqual([{ uri: 'tos://strange/datasets/demo_v2', name: 'demo_v2' }]);
    expect(plan.datasets[0].datasetId).toBeUndefined();
    // No registry at all (like v1's synthesized single source): still used as given, never guessed
    const plan2 = planDeepLink(q('dataset=tos://curation/datasets/demo_v2'), ctx([]));
    expect(plan2.datasets).toEqual([{ uri: 'tos://curation/datasets/demo_v2', name: 'demo_v2' }]);
  });

  it('test_deeplink_known_bucket_missing_dataset_says_so: an unknown bare name is named in the notice', () => {
    const plan = planDeepLink(q('dataset=nope'), ctx([reg('ds_a', 'tos://curation/datasets/demo_v2')]));
    expect(plan.datasets).toEqual([]);
    expect(plan.notes.dataset.some((n) => n.includes('nope') && n.includes('找不到'))).toBe(true);
  });

  it('test_deeplink_prefix_mismatch_is_rejected_not_matched: another prefix never matches the registration', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/droid_lerobot')];
    const plan = planDeepLink(q('dataset=tos://curation/raw/droid_lerobot'), ctx(registered));
    expect(plan.datasets).toEqual([{ uri: 'tos://curation/raw/droid_lerobot', name: 'droid_lerobot' }]);
    // at the bucket root: not the registered datasets/ one either
    const plan2 = planDeepLink(q('dataset=tos://curation/droid_lerobot'), ctx(registered));
    expect(plan2.datasets[0].datasetId).toBeUndefined();
    // trailing slash tolerated: the registered one is recognised
    const plan3 = planDeepLink(q('dataset=tos://curation/datasets/droid_lerobot/'), ctx(registered));
    expect(plan3.datasets[0].datasetId).toBe('ds_a');
  });

  it('test_deeplink_unknown_local_prefix_never_admits_match: registrations of other sources never match', () => {
    const registered = [reg('ds_local', '/mnt/data/demo_v2', null, 'local'), reg('ds_pub', 'tos://hf-cache/lerobot/demo_v2', null, 'public')];
    const plan = planDeepLink(q('dataset=tos://curation/datasets/demo_v2'), ctx(registered));
    expect(plan.datasets[0].datasetId).toBeUndefined();
    const bare = planDeepLink(q('dataset=demo_v2'), ctx(registered));
    expect(bare.datasets).toEqual([]);
    expect(bare.notes.dataset.length).toBeGreaterThan(0);
  });

  it('test_deeplink_bare_name_keeps_todays_behavior: a bare name is found among registered datasets', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2'), reg('ds_o', 'tos://curation/datasets/other')];
    const plan = planDeepLink(q('dataset=demo_v2'), ctx(registered));
    expect(plan.datasets).toEqual([{ uri: 'tos://curation/datasets/demo_v2', name: 'demo_v2', datasetId: 'ds_a', region: 'cn-beijing' }]);
    expect(plan.notes.dataset).toEqual([]);
    const miss = planDeepLink(q('dataset=demo_v2,gone'), ctx(registered));
    expect(miss.datasets.map((d) => d.name)).toEqual(['demo_v2']); // the one that exists is still selected
    expect(miss.notes.dataset).toEqual([
      '链接里的数据集在本站找不到：gone（「数据集」里没有登记同名的数据集，可以直接填完整的 tos:// 地址）',
    ]);
  });

  it('a bare name registered under two addresses is never guessed', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2'), reg('ds_b', 'tos://bucketa/prefix/demo_v2')];
    const plan = planDeepLink(q('dataset=demo_v2'), ctx(registered));
    expect(plan.datasets).toEqual([]);
    expect(plan.notes.dataset[0]).toContain('同名');
  });

  it.each([[''], ['tos://'], ['tos://bucketonly'], ['https://not-tos/x'], ['tos://bkt/datasets/demo%20v2'], ['tos://b/datasets/demo%20v2']])(
    'test_deeplink_never_silent_when_nothing_selected: %j',
    (value) => {
      const plan = planDeepLink(q(`dataset=${encodeURIComponent(value)}`), ctx([reg('ds_a', 'tos://curation/datasets/demo_v2')]));
      expect(plan.present).toBe(true);
      expect(plan.datasets).toEqual([]);
      expect(plan.notes.dataset.length).toBeGreaterThan(0);
    },
  );

  it('test_deeplink_refs_spanning_two_sources_abstain_with_notice: bare names in two places abstain', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/only_a'), reg('ds_b', 'tos://bucketa/prefix/only_b')];
    const plan = planDeepLink(q('dataset=only_a,only_b'), ctx(registered));
    expect(plan.datasets).toEqual([]);
    expect(plan.notes.dataset.some((n) => n.includes('分属不同存储桶或前缀'))).toBe(true);
  });

  it('several tos:// URLs: only the first prefix is used and the rest are named (v1 app rule)', () => {
    const plan = planDeepLink(q('dataset=tos://curation/datasets/only_a,tos://bucketa/prefix/only_b,tos://curation/datasets/b2'), ctx([]));
    expect(plan.datasets.map((d) => d.uri)).toEqual(['tos://curation/datasets/only_a', 'tos://curation/datasets/b2']);
    expect(plan.notes.dataset).toEqual(['链接带了多个不同前缀，只认第一个（tos://curation/datasets）；已忽略 tos://bucketa/prefix/only_b']);
  });

  it('test_deeplink_info_uses_display_label: a successful pre-selection is silent', () => {
    const plan = planDeepLink(q('dataset=tos://cust-a-data/x/demo_v2'), ctx([reg('ds_a', 'tos://cust-a-data/x/demo_v2')]));
    expect(plan.datasets).toHaveLength(1);
    expect(plan.notes).toEqual({ dataset: [], region: [] });
  });

  it('test_deeplink_without_endpoint_four_wordings_verbatim: no endpoint → no endpoint wording anywhere', () => {
    const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2')];
    for (const s of ['dataset=nope', 'dataset=tos://strange/datasets/demo_v2', 'dataset=tos://curation/raw/demo_v2']) {
      const plan = planDeepLink(q(s), ctx(registered));
      expect(plan.notes.dataset.join('')).not.toContain('端点');
      expect(plan.notes.region).toEqual([]);
    }
  });

  it('bucket-only link fills the root and says why nothing is selected', () => {
    const plan = planDeepLink(q('dataset=tos://bucketonly'), ctx([]));
    expect(plan.rootOnly).toBe('tos://bucketonly');
    expect(plan.datasets).toEqual([]);
    expect(plan.notes.dataset[0]).toContain('没有指到具体的数据集');
  });

  it('several datasets under one prefix → batch mode (P3)', () => {
    const plan = planDeepLink(q('dataset=tos://bkt/ds/a&url=tos://bkt/ds/b'), ctx([]));
    expect(plan.datasets.map((d) => d.name)).toEqual(['a', 'b']);
  });
});

describe('source=public (retired test_public_deeplink_preselects_and_switches_source)', () => {
  const publicDatasets = [
    { name: 'libero', uri: 'tos://hf-cache/lerobot/libero' },
    { name: 'pusht', uri: 'tos://hf-cache/lerobot/pusht' },
  ];

  it('switches the source and pre-selects by name', () => {
    const plan = planDeepLink(q('source=public&dataset=libero'), ctx([], { publicDatasets }));
    expect(plan.source).toBe('public');
    expect(plan.datasets).toEqual([{ uri: 'tos://hf-cache/lerobot/libero', name: 'libero' }]);
    expect(plan.notes.dataset).toEqual([]);
  });

  it('without a dataset only switches the source', () => {
    const plan = planDeepLink(q('source=PUBLIC'), ctx([], { publicDatasets }));
    expect(plan.source).toBe('public');
    expect(plan.datasets).toEqual([]);
    expect(plan.notes.dataset).toEqual([]);
  });

  it('an unknown public name is named', () => {
    const plan = planDeepLink(q('source=public&dataset=gone'), ctx([], { publicDatasets }));
    expect(plan.notes.dataset[0]).toContain('gone');
  });

  it('a tos:// link into the cache bucket switches the source too', () => {
    const plan = planDeepLink(q('dataset=tos://hf-cache/lerobot/pusht'), ctx([], { publicDatasets }));
    expect(plan.source).toBe('public');
    expect(plan.datasets).toEqual([{ uri: 'tos://hf-cache/lerobot/pusht', name: 'pusht' }]);
  });

  it('a site without a cache bucket says so instead of ignoring the key', () => {
    const plan = planDeepLink(q('source=public&dataset=libero'), ctx([]));
    expect(plan.source).not.toBe('public');
    expect(plan.notes.dataset[0]).toContain('HuggingFace 缓存桶');
  });
});

describe('region', () => {
  it('takes the first valid value, lowercased; bad values are dropped and never echoed', () => {
    expect(deeplinkRegion(q('region=CN-Beijing'))).toEqual({ region: 'cn-beijing', present: true });
    expect(deeplinkRegion(q('region=%3Cx%3E&region=cn-shanghai'))).toEqual({ region: 'cn-shanghai', present: true });
    expect(deeplinkRegion(q('region=<script>'))).toEqual({ region: null, present: true });
    expect(deeplinkRegion(q('region='))).toEqual({ region: null, present: true });
    expect(deeplinkRegion(q('x=1'))).toEqual({ region: null, present: false });
    const plan = planDeepLink(q('dataset=tos://bkt/ds/a&region=<script>alert(1)</script>'), ctx([]));
    expect(plan.region).toBeNull();
    expect(plan.notes.region).toHaveLength(1);
    expect(plan.notes.region[0]).not.toContain('<script>');
  });

  it('a region alone still pre-selects', () => {
    const plan = planDeepLink(q('region=cn-shanghai'), ctx([]));
    expect(plan.present).toBe(true);
    expect(plan.region).toBe('cn-shanghai');
    expect(plan.notes.dataset).toEqual([]);
  });
});

describe('endpoint (hints only)', () => {
  const registered = [reg('ds_a', 'tos://curation/datasets/demo_v2', 'cn-beijing')];

  it('test_deeplink_endpoint_same_region_cross_network_domain_is_silent', () => {
    const plan = planDeepLink(q('dataset=tos://curation/datasets/demo_v2&endpoint=tos-cn-beijing.volces.com'), ctx(registered));
    expect(plan.datasets[0].datasetId).toBe('ds_a');
    expect(plan.notes.region).toEqual([]);
    const plan2 = planDeepLink(q('dataset=tos://curation/datasets/demo_v2&endpoint=tos-cn-beijing.ivolces.com'), ctx(registered));
    expect(plan2.notes.region).toEqual([]);
  });

  it('test_deeplink_endpoint_region_conflict_names_both_regions_keeps_selection', () => {
    const plan = planDeepLink(q('dataset=tos://curation/datasets/demo_v2&endpoint=tos-ap-southeast-1.volces.com'), ctx(registered));
    expect(plan.datasets[0].datasetId).toBe('ds_a'); // pre-selection unaffected
    const conflicts = plan.notes.region.filter((n) => n.includes('ap-southeast-1') && n.includes('cn-beijing'));
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]).toContain('tos-ap-southeast-1.volces.com');
    // Several datasets: the contradiction is said once
    const plan2 = planDeepLink(
      q('dataset=tos://curation/datasets/demo_v2,tos://curation/datasets/x2&region=cn-beijing&endpoint=tos-ap-southeast-1.volces.com'),
      ctx(registered),
    );
    expect(plan2.notes.region.filter((n) => n.includes('不一致'))).toHaveLength(1);
  });

  it('test_deeplink_endpoint_appended_to_unmatched_bucket_notice: the unmatched notice names the endpoint', () => {
    const plan = planDeepLink(q('dataset=gone&endpoint=tos-cn-shanghai.volces.com'), ctx(registered));
    expect(plan.notes.dataset).toHaveLength(1);
    expect(plan.notes.dataset[0].startsWith('链接里的数据集在本站找不到：gone')).toBe(true);
    expect(plan.notes.dataset[0]).toContain('tos-cn-shanghai.volces.com');
  });

  it('test_deeplink_endpoint_two_key_names_both_recognized', () => {
    const host = 'tos-cn-beijing.volces.com';
    expect(deeplinkEndpoint({ endpoint: host })).toEqual({ host, present: true });
    expect(deeplinkEndpoint({ tos_endpoint: `https://${host}` })).toEqual({ host, present: true });
    expect(deeplinkEndpoint({ endpoint: 'a.volces.com', tos_endpoint: 'b.volces.com' })).toEqual({ host: 'a.volces.com', present: true });
    expect(deeplinkEndpoint({ endpoint: '<img onerror=x>', tos_endpoint: 'b.volces.com' })).toEqual({ host: 'b.volces.com', present: true });
    expect(deeplinkEndpoint({ endpoint: '<img onerror=x>' })).toEqual({ host: null, present: true });
    expect(deeplinkEndpoint({})).toEqual({ host: null, present: false });
    expect(deeplinkEndpoint({ endpoint: [host] })).toEqual({ host, present: true });
  });

  it('test_endpoint_sanitizer_strips_or_rejects_untrusted_input', () => {
    expect(sanitizeEndpoint('https://u:p@tos-cn-beijing.volces.com/a/b?c=1')).toBe('tos-cn-beijing.volces.com');
    expect(sanitizeEndpoint('tos-cn-beijing.ivolces.com:443/bucket')).toBe('tos-cn-beijing.ivolces.com');
    expect(sanitizeEndpoint('TOS-CN-BEIJING.VOLCES.COM')).toBe('tos-cn-beijing.volces.com');
    const evils = [
      '<img onerror=alert(1)>',
      '[x](javascript:alert(1))',
      '[x](https://evil.com/a)',
      'a'.repeat(2000),
      `${'b'.repeat(300)}.volces.com`,
      'host name with spaces',
      '',
    ];
    for (const evil of evils) {
      const got = sanitizeEndpoint(evil);
      expect(got === null || /^[a-z0-9.-]{1,253}$/.test(got)).toBe(true);
      expect(got).not.toBe(evil.toLowerCase());
    }
    // Pipeline level: a dirty endpoint never reaches any note verbatim
    for (const evil of evils.slice(0, 3)) {
      const params = new URLSearchParams();
      params.set('endpoint', evil);
      params.set('dataset', 'gone');
      const plan = planDeepLink(params, ctx(registered));
      expect(plan.present).toBe(true);
      expect([...plan.notes.dataset, ...plan.notes.region].every((n) => !n.includes(evil))).toBe(true);
      expect(plan.notes.region).toContain('链接里的端点参数看不懂，已忽略（不影响预选）');
    }
  });

  it('test_endpoint_region_unrecognized_skips_conflict_and_never_crashes', () => {
    expect(endpointRegion('tos-cn-beijing.volces.com')).toBe('cn-beijing');
    expect(endpointRegion('tos-ap-southeast-1.ivolces.com')).toBe('ap-southeast-1');
    expect(endpointRegion('tos-s3-cn-north-1.volces.com')).toBe('cn-north-1');
    expect(endpointRegion('example.com')).toBeNull();
    expect(endpointRegion(null)).toBeNull();
    const plan = planDeepLink(q('dataset=tos://curation/datasets/demo_v2&endpoint=example.com'), ctx(registered));
    expect(plan.datasets).toHaveLength(1);
    expect(plan.notes.region).toEqual([]);
    // Nothing to compare with (no region anywhere): stay quiet
    const plan2 = planDeepLink(q('dataset=tos://curation/datasets/demo_v2&endpoint=tos-ap-southeast-1.volces.com'), ctx([]));
    expect(plan2.notes.region).toEqual([]);
  });
});

describe('splitDatasetUrl (test_tos_merge.py)', () => {
  it('test_split_dataset_url_last_segment_rule', () => {
    expect(splitDatasetUrl('tos://curation/datasets/droid_lerobot')).toEqual(['tos://curation/datasets', 'droid_lerobot']);
    expect(splitDatasetUrl('tos://bkt/a/b/c')).toEqual(['tos://bkt/a/b', 'c']);
    expect(splitDatasetUrl('tos://bkt/pusht')).toEqual(['tos://bkt', 'pusht']);
  });

  it('test_split_dataset_url_bucket_only_means_no_preselect', () => {
    expect(splitDatasetUrl('tos://bkt')).toEqual(['tos://bkt', '']);
  });

  it('test_split_dataset_url_rejects_bad_syntax', () => {
    expect(() => splitDatasetUrl('s3://bkt/x')).toThrow();
    expect(() => splitDatasetUrl('tos://bkt/../etc')).toThrow();
  });
});

describe('delivery directory defaults (test_tos_probe / test_public_catalog)', () => {
  it('test_borrowed_output_url: last root first, else the dataset bucket', () => {
    expect(borrowedOutputUrl('tos://their-bucket/dataset-1', null)).toBe('tos://their-bucket/deliveries');
    expect(borrowedOutputUrl('/data/datasets', null)).toBe(''); // not tos
    expect(borrowedOutputUrl('', null)).toBe('');
    expect(borrowedOutputUrl('tos://', null)).toBe(''); // unparsable
    // «own bucket» in v2 = the delivery root used last time
    expect(borrowedOutputUrl('tos://their-bucket/dataset-1', 'tos://herbucket/deliveries/')).toBe('tos://herbucket/deliveries');
  });

  it('test_runner_never_borrows_public_bucket_and_public_output_default', () => {
    expect(borrowedOutputUrl('tos://hf-cache/dataset/libero', null, ['hf-cache'])).toBe('');
    expect(borrowedOutputUrl('tos://their-bucket/x', null, ['hf-cache'])).toBe('tos://their-bucket/deliveries');
    expect(publicOutputDefault(null)).toEqual(['', '公共数据集只读，交付目录请填你有写权限的 tos://存储桶名/目录']);
    expect(publicOutputDefault('tos://mine/deliveries')).toEqual(['tos://mine/deliveries', '']);
  });

  it('suggestDeliveryName follows v1 (<dataset>-<MMDD>, cleaned)', () => {
    expect(suggestDeliveryName('droid_100', '0921')).toBe('droid_100-0921');
    expect(suggestDeliveryName('我的 数据', '0921')).toBe('dataset-0921');
    expect(suggestDeliveryName('.hidden set', '0101')).toBe('hidden-set-0101');
  });
});
