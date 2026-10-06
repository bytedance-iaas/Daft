import { Menu, Message } from '@arco-design/web-react';
import { IconCode, IconDashboard, IconList, IconLock, IconQuestionCircle, IconStorage } from '@arco-design/web-react/icon';
import { Suspense, useState, type ReactNode } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Spin } from '@arco-design/web-react';
import { appPath } from '../base';
import { readPrefs, writePrefs } from '../lib/prefs';
import { zh } from '../locales/zh';

interface NavItem {
  key: string;
  label: string;
  icon?: ReactNode;
  children?: readonly NavItem[];
}

const QC_KEY = 'qc';
const DS_KEY = 'ds';

const NAV: readonly NavItem[] = [
  { key: '/overview', label: zh.nav.overview, icon: <IconDashboard /> },
  {
    key: QC_KEY,
    label: zh.nav.qc,
    icon: <IconList />,
    children: [
      { key: '/tasks', label: zh.nav.tasks },
      { key: '/adjudication', label: zh.nav.adjudication },
    ],
  },
  {
    key: DS_KEY,
    label: zh.nav.datasets,
    icon: <IconStorage />,
    children: [
      { key: '/datasets', label: zh.nav.datasetList },
      { key: '/visualize', label: zh.nav.visualize },
    ],
  },
  { key: '/credentials', label: zh.nav.credentials, icon: <IconLock /> },
];

/** The entry a path belongs to: a task's adjudication page sits under 人工裁决, its report under 质检任务. */
export function navKey(pathname: string): string {
  if (/^\/tasks\/[^/]+\/adjudication\/?$/.test(pathname)) return '/adjudication';
  const keys = NAV.flatMap((n) => (n.children ? n.children.map((c) => c.key) : [n.key]));
  return keys.find((k) => pathname === k || pathname.startsWith(`${k}/`)) ?? '/overview';
}

/**
 * Where the 「帮助」 entries lead (doc 07 §2); the only place these addresses are written. A full
 * URL opens as it is, a path is taken under the mount prefix (so `/curation/api-docs.html` in
 * production). An empty one makes the entry only say it is not configured.
 * - docs, 使用文档: the guide for the people who check data; empty until it is published.
 * - api, 接口文档: the API reference built next to the console (api-docs.html, Scalar over C4).
 */
export const HELP_LINKS = { docs: '', api: '/api-docs.html' };

/** The address a help link opens. */
export function helpUrl(link: string): string {
  return /^[a-z][a-z0-9+.-]*:/i.test(link) ? link : appPath(link);
}

const HELP: readonly { key: string; link: keyof typeof HELP_LINKS; label: string; missing: string; icon: ReactNode }[] = [
  { key: 'help:docs', link: 'docs', label: zh.nav.docs, missing: zh.nav.docsMissing, icon: <IconQuestionCircle /> },
  { key: 'help:api', link: 'api', label: zh.nav.apiDocs, missing: zh.nav.apiDocsMissing, icon: <IconCode /> },
];

/** Opens a help entry in a new tab; false when `key` is not one. */
function openHelp(key: string): boolean {
  const item = HELP.find((h) => h.key === key);
  if (!item) return false;
  const link = HELP_LINKS[item.link];
  if (link) window.open(helpUrl(link), '_blank', 'noopener,noreferrer');
  else Message.info(item.missing);
  return true;
}

/**
 * Header + sidebar (概览、质检 with 质检任务 / 人工裁决、数据集 with 数据集列表 / 可视化、
 * 系统和资源配置, then 帮助 with 使用文档 / 接口文档; doc 07 §2, design doc 18 §5.0) around the
 * routed page. The sidebar collapses to its icons (the button at its bottom; remembered in this browser),
 * where a group's entries open as a popup.
 */
export function AppLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const selected = navKey(pathname);
  const [collapsed, setCollapsed] = useState(() => readPrefs().siderCollapsed === true);
  return (
    <>
      <header className="app-header">
        <span className="app-logo" aria-hidden>
          <svg width="16" height="16" viewBox="0 0 32 32">
            <path d="M9 17.5l4.5 4.5L23 11" fill="none" stroke="#fff" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <span className="app-vendor">{zh.app.vendor}</span>
        <span className="app-product">{zh.app.product}</span>
      </header>
      <div className="app-body">
        <nav className={collapsed ? 'app-sider collapsed' : 'app-sider'} aria-label={zh.nav.groupMain}>
          <Menu
            collapse={collapsed}
            hasCollapseButton
            onCollapseChange={(c) => {
              setCollapsed(c);
              writePrefs({ siderCollapsed: c });
            }}
            selectedKeys={[selected]}
            defaultOpenKeys={[QC_KEY, DS_KEY]}
            onClickMenuItem={(key) => {
              if (!openHelp(key)) navigate(key);
            }}
          >
            <Menu.ItemGroup title={zh.nav.groupMain}>
              {NAV.map((n) =>
                n.children ? (
                  <Menu.SubMenu
                    key={n.key}
                    title={
                      <>
                        {n.icon}
                        {n.label}
                      </>
                    }
                  >
                    {n.children.map((c) => (
                      <Menu.Item key={c.key}>{c.label}</Menu.Item>
                    ))}
                  </Menu.SubMenu>
                ) : (
                  <Menu.Item key={n.key}>
                    {n.icon}
                    {n.label}
                  </Menu.Item>
                ),
              )}
            </Menu.ItemGroup>
            <Menu.ItemGroup title={zh.nav.groupHelp}>
              {HELP.map((h) => (
                <Menu.Item key={h.key}>
                  {h.icon}
                  {h.label}
                </Menu.Item>
              ))}
            </Menu.ItemGroup>
          </Menu>
        </nav>
        <main className="app-main">
          <Suspense fallback={<Spin style={{ display: 'block', margin: '80px auto' }} />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </>
  );
}
