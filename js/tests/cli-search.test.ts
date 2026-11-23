import { describe, expect, it } from 'vitest';
import { cmdSearch } from '../src/cli.js';
import type { SnoopScan } from '../src/client.js';

/**
 * `search --scrape` must send `scrapeOptions`, an object. The API's strict
 * SearchRequest model rejects a bare `scrapeResults: true` with "Extra inputs
 * are not permitted". The Python CLI is tested for the same.
 */
function stubClient(): { calls: Array<{ query: string; body: Record<string, unknown> }>; client: SnoopScan } {
  const calls: Array<{ query: string; body: Record<string, unknown> }> = [];
  const client = {
    search: async (query: string, body: Record<string, unknown> = {}) => {
      calls.push({ query, body });
      return { results: [], provider: 'test' };
    },
  } as unknown as SnoopScan;
  return { calls, client };
}

describe('cmdSearch', () => {
  it('sends scrapeOptions, not scrapeResults, when --scrape is passed', async () => {
    const { calls, client } = stubClient();
    await cmdSearch(client, ['widgets', '--scrape', '--json']);
    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call?.body).toHaveProperty('scrapeOptions');
    expect(call?.body).not.toHaveProperty('scrapeResults');
  });

  it('sends no scrapeOptions without --scrape', async () => {
    const { calls, client } = stubClient();
    await cmdSearch(client, ['widgets', '--json']);
    const [call] = calls;
    expect(call?.body).not.toHaveProperty('scrapeOptions');
  });
});
