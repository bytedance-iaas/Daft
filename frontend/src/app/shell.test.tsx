import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { HELP_LINKS } from '../components/AppLayout';
import { currentLocation, renderApp } from '../test/render';

afterEach(() => {
  delete window.__CURATOR_BASE__;
  window.history.pushState({}, '', '/');
  HELP_LINKS.docs = '';
  vi.restoreAllMocks();
});

describe('app shell', () => {
  it('has the sidebar entries in the requested order, 帮助 last, and opens 概览 by default', async () => {
    renderApp('/');
    const nav = await screen.findByRole('navigation', { name: '数据质检' });
    const items = within(nav).getAllByRole('menuitem').map((el) => el.textContent);
    expect(items).toEqual(['概览', '质检任务', '质检报告', '人工裁决', '数据集', '系统和资源配置', '使用文档']);
    expect(within(nav).getByText('帮助')).toBeInTheDocument();
    // 质检 holds the three, open from the start (requester, third round)
    expect(within(nav).getByText('质检')).toBeInTheDocument();
    await waitFor(() => expect(currentLocation()).toBe('/overview'));
  });

  it('质检报告 and 人工裁决 light up on their lists and on a task\'s report and adjudication pages', async () => {
    const { user } = renderApp('/tasks');
    const nav = await screen.findByRole('navigation', { name: '数据质检' });
    const selected = () => nav.querySelector('.arco-menu-item.arco-menu-selected')?.textContent;
    await waitFor(() => expect(selected()).toBe('质检任务'));
    await user.click(within(nav).getByRole('menuitem', { name: '质检报告' }));
    await waitFor(() => expect(currentLocation()).toBe('/reports'));
    expect(selected()).toBe('质检报告');
    await user.click(await screen.findByRole('link', { name: 'droid 前 50 条质检' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/task_01HXR2D8/report'));
    expect(selected()).toBe('质检报告');
    await user.click(within(nav).getByRole('menuitem', { name: '人工裁决' }));
    await waitFor(() => expect(currentLocation()).toBe('/adjudication'));
    await user.click(await screen.findByRole('link', { name: 'droid 前 50 条质检' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/task_01HXR2D8/adjudication'));
    expect(selected()).toBe('人工裁决');
  });

  it('使用文档 says it is not configured while its URL is empty, and opens it in a new tab once set', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = renderApp('/tasks');
    const nav = await screen.findByRole('navigation', { name: '数据质检' });
    await user.click(within(nav).getByRole('menuitem', { name: '使用文档' }));
    expect(await screen.findByText('使用文档还没配置')).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    expect(currentLocation()).toBe('/tasks');
    HELP_LINKS.docs = 'https://docs.example.com/curator';
    await user.click(within(nav).getByRole('menuitem', { name: '使用文档' }));
    expect(open).toHaveBeenCalledWith('https://docs.example.com/curator', '_blank', 'noopener,noreferrer');
    expect(currentLocation()).toBe('/tasks');
  });

  it('the breadcrumb root 「数据质检平台」 leads to 概览', async () => {
    const { user } = renderApp('/tasks');
    const root = await screen.findByRole('link', { name: '数据质检平台' });
    expect(root).toHaveAttribute('href', '/overview');
    await user.click(root);
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
