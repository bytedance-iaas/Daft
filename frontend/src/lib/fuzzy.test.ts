import { describe, expect, it } from 'vitest';
import { fuzzyFilter, fuzzyMatch } from './fuzzy';

const MODELS = ['doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-260215', 'doubao-seed-1-6-251015', 'ep-20260915173012-x7k2p', 'Qwen2.5-VL-72B-Instruct'];

describe('typing a few letters to find a model (07 §7)', () => {
  it('matches letters in order, ignoring case and spaces', () => {
    expect(fuzzyMatch('doubao-seed-2-0-lite-260215', 'd20lite')).toBe(true);
    expect(fuzzyMatch('doubao-seed-2-0-lite-260215', 'd 2 0 lite')).toBe(true);
    expect(fuzzyMatch('Qwen2.5-VL-72B-Instruct', 'qwenvl')).toBe(true);
    expect(fuzzyMatch('doubao-seed-2-0-pro-260215', 'lite')).toBe(false);
    // Order matters, and an empty query matches everything.
    expect(fuzzyMatch('doubao-seed-2-0-lite-260215', 'etil')).toBe(false);
    expect(fuzzyMatch('anything', '  ')).toBe(true);
  });

  it('filters a list, the plain substring hits first', () => {
    expect(fuzzyFilter(MODELS, 'lite', (m) => m)).toEqual(['doubao-seed-2-0-lite-260215']);
    expect(fuzzyFilter(MODELS, 'd-2-0', (m) => m)).toEqual(['doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-260215']);
    // 「d20」 matches 1-6-251015 as well: d, then 2 and 0 somewhere after it.
    expect(fuzzyFilter(MODELS, 'd20', (m) => m)).toEqual(['doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-260215', 'doubao-seed-1-6-251015']);
    expect(fuzzyFilter(MODELS, 'ep-2026', (m) => m)).toEqual(['ep-20260915173012-x7k2p']);
    expect(fuzzyFilter(MODELS, '260215', (m) => m)).toEqual(['doubao-seed-2-0-pro-260215', 'doubao-seed-2-0-lite-260215']);
    expect(fuzzyFilter(MODELS, 'zzz', (m) => m)).toEqual([]);
    expect(fuzzyFilter(MODELS, '', (m) => m)).toEqual(MODELS);
  });
});
