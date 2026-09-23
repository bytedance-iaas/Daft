import { Menu, Message } from '@arco-design/web-react';
import { IconDashboard, IconList, IconLock, IconQuestionCircle, IconStorage } from '@arco-design/web-react/icon';
import { Suspense } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Spin } from '@arco-design/web-react';
import { zh } from '../locales/zh';

const NAV = [
  { key: '/overview', label: zh.nav.overview, icon: <IconDashboard /> },
  { key: '/tasks', label: zh.nav.tasks, icon: <IconList /> },
  { key: '/datasets', label: zh.nav.datasets, icon: <IconStorage /> },
  { key: '/credentials', label: zh.nav.credentials, icon: <IconLock /> },
];

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

/** Header + sidebar (概览、质检任务、数据集、密钥与资源, then 帮助; doc 07 §2) around the routed page. */
export function AppLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const selected = NAV.find((n) => pathname === n.key || pathname.startsWith(`${n.key}/`))?.key ?? '/overview';
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
          <Menu selectedKeys={[selected]} onClickMenuItem={(key) => (key === DOCS_KEY ? openDocs() : navigate(key))}>
            <Menu.ItemGroup title={zh.nav.groupMain}>
              {NAV.map((n) => (
                <Menu.Item key={n.key}>
                  {n.icon}
                  {n.label}
                </Menu.Item>
              ))}
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
