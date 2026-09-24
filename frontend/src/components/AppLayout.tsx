import { Menu, Message } from '@arco-design/web-react';
import { IconDashboard, IconList, IconLock, IconQuestionCircle, IconStorage } from '@arco-design/web-react/icon';
import { Suspense, type ReactNode } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Spin } from '@arco-design/web-react';
import { zh } from '../locales/zh';

interface NavItem {
  key: string;
  label: string;
  icon?: ReactNode;
  children?: readonly NavItem[];
}

const QC_KEY = 'qc';

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
  { key: '/datasets', label: zh.nav.datasets, icon: <IconStorage /> },
  { key: '/credentials', label: zh.nav.credentials, icon: <IconLock /> },
];

/** The entry a path belongs to: a task's adjudication page sits under 人工裁决, its report under 质检任务. */
export function navKey(pathname: string): string {
  if (/^\/tasks\/[^/]+\/adjudication\/?$/.test(pathname)) return '/adjudication';
  const keys = NAV.flatMap((n) => (n.children ? n.children.map((c) => c.key) : [n.key]));
  return keys.find((k) => pathname === k || pathname.startsWith(`${k}/`)) ?? '/overview';
}

/**
 * Where 「帮助 · 使用文档」 leads. Empty until the user guide is published: the entry then only
 * says so. Set it to the guide's URL and the entry opens it in a new tab.
 */
export const HELP_LINKS = { docs: '' };

const DOCS_KEY = 'help:docs';

function openDocs(): void {
  if (!HELP_LINKS.docs) {
    Message.info(zh.nav.docsMissing);
    return;
  }
  window.open(HELP_LINKS.docs, '_blank', 'noopener,noreferrer');
}

/**
 * Header + sidebar (概览、质检 with 质检任务 / 人工裁决、数据集、系统和资源配置, then 帮助;
 * doc 07 §2) around the routed page.
 */
export function AppLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const selected = navKey(pathname);
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
        <nav className="app-sider" aria-label={zh.nav.groupMain}>
          <Menu
            selectedKeys={[selected]}
            defaultOpenKeys={[QC_KEY]}
            onClickMenuItem={(key) => (key === DOCS_KEY ? openDocs() : navigate(key))}
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
              <Menu.Item key={DOCS_KEY}>
                <IconQuestionCircle />
                {zh.nav.docs}
              </Menu.Item>
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
