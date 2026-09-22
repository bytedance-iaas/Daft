import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { currentLocation, renderApp } from '../test/render';

afterEach(() => {
  delete window.__CURATOR_BASE__;
  window.history.pushState({}, '', '/');
});

describe('app shell', () => {
  it('has the four sidebar entries in the reviewed order and opens 概览 by default', async () => {
    renderApp('/');
    const nav = await screen.findByRole('navigation', { name: '数据质检' });
    const items = within(nav).getAllByRole('menuitem').map((el) => el.textContent);
    expect(items).toEqual(['概览', '数据集', '质检任务', '密钥与资源']);
    await waitFor(() => expect(currentLocation()).toBe('/overview'));
  });

  it('forwards v1 deep links on / to /tasks/new with the query untouched', async () => {
    renderApp('/?dataset=tos://bkt/ds/droid&region=cn-beijing');
    await waitFor(() => expect(currentLocation()).toBe('/tasks/new?dataset=tos://bkt/ds/droid&region=cn-beijing'));
  });

  it('forwards endpoint- or source-only deep links too', async () => {
    renderApp('/?source=public');
    await waitFor(() => expect(currentLocation()).toBe('/tasks/new?source=public'));
  });

  it('shows a Chinese 404 for unknown routes', async () => {
    renderApp('/nope/here');
    expect(await screen.findByText('这个页面不存在')).toBeInTheDocument();
  });

  it('works under the /curation mount prefix, from any deep route (refresh does not 404)', async () => {
    window.__CURATOR_BASE__ = '/curation';
    window.history.pushState({}, '', '/curation/tasks');
    render(<App />);
    expect(await screen.findByRole('heading', { name: /质检任务/ })).toBeInTheDocument();
    window.history.pushState({}, '', '/curation/credentials');
  });
});
