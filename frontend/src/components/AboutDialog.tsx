import { Modal } from '@arco-design/web-react';
import { absoluteTime } from '../lib/format';
import { zh } from '../locales/zh';

declare const __CURATOR_BUILD__: { version: string; revision: string; builtAt: string };

/** What the build baked in (vite.config.ts `buildInfo`): the version, the commit when known, when it was built. */
export const BUILD = __CURATOR_BUILD__;

/** 「关于」 (2026-10-13): the product, its version, the commit it was built from and when. */
export function AboutDialog({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const A = zh.about;
  return (
    <Modal title={A.title} visible={visible} onCancel={onClose} footer={null} style={{ width: 420 }} unmountOnExit>
      <div data-testid="about">
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>{zh.app.product}</div>
        <div className="muted" style={{ marginBottom: 16 }}>
          {zh.app.vendor}
        </div>
        <dl className="kv" style={{ margin: 0 }}>
          <dt>{A.version}</dt>
          <dd className="mono" data-testid="about-version">
            {BUILD.version ? `v${BUILD.version}` : '—'}
          </dd>
          <dt>{A.revision}</dt>
          <dd className="mono" data-testid="about-revision" title={BUILD.revision || undefined}>
            {BUILD.revision ? BUILD.revision.slice(0, 9) : <span className="muted">{A.revisionUnknown}</span>}
          </dd>
          <dt>{A.builtAt}</dt>
          <dd>{absoluteTime(Date.parse(BUILD.builtAt))}</dd>
        </dl>
      </div>
    </Modal>
  );
}
