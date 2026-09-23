import { describe, expect, it } from 'vitest';
import { jsonlToArray, uploadText } from './uploads';
import { ApiError } from './errors';

describe('uploads (C4 1.8.0, F5.5)', () => {
  it('turns a JSONL seed file into the JSON array the Daemon accepts', () => {
    expect(JSON.parse(jsonlToArray('{"a":1}\r\n\n  {"a":2}  \n'))).toEqual([{ a: 1 }, { a: 2 }]);
    expect(() => jsonlToArray('{"a":1}\n{oops')).toThrow('第 2 行不是合法的 JSON');
  });

  it('sends the file as the JSON body and returns the handle; a rejected file keeps its located errors', async () => {
    const traj = JSON.stringify({ schema_version: 'eef-video/1.0.0', samples: [{ episode_index: 3, sample: { sample_id: 's3' }, frames: [{}, {}] }] });
    const up = await uploadText(traj, 'eef_trajectory', 'trajectory.json');
    expect(up.handle).toBe(`upload:${up.upload_id}`);
    expect(up.validation.summary).toMatchObject({ samples: 1, episodes: [3], frames: 2 });
    const seeds = await uploadText('{"sample_id":"s3"}\n{"sample_id":"s4"}\n', 'eef_observation_seeds', 'seeds.jsonl');
    expect(seeds.validation.summary).toMatchObject({ rows: 2, samples: 2 });
    const bad = JSON.stringify({ samples: [{ episode_index: 0, sample: { sample_id: 's0' }, frames: [{ truth: [1, 2] }] }] });
    const err = await uploadText(bad, 'eef_trajectory', 'trajectory.json').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).details).toMatchObject({ errors: [{ code: 'forbidden_key', sample_id: 's0' }] });
  });
});
