import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { findDrawer } from '../../test/arco';
import { renderApp } from '../../test/render';

const T = zh.declaration;

describe('数据集声明 (design doc 25 §3, F5.23)', () => {
  it('drafts from the dataset, says what is missing, and confirming the filled draft makes version 1', async () => {
    const { user } = renderApp('/datasets/ds_droid100?declaration=1');
    const drawer = await findDrawer('数据集声明 · droid_100');
    expect(await within(drawer).findByTestId('decl-draft-note')).toBeInTheDocument();
    const status = within(drawer).getByTestId('decl-status');
    expect(within(status).getByTestId('decl-missing')).toHaveTextContent(T.missing.pose_frame_unknown);
    expect(within(status).getByTestId('decl-missing')).toHaveTextContent(T.missing.tool_missing);
    expect(within(drawer).getByTestId('decl-reason-exterior_image_1_left')).toHaveTextContent(T.reasons.intrinsics_missing);
    const input = (id: string) => {
      const el = within(drawer).getByTestId(id);
      return el.tagName === 'INPUT' ? el : (el.querySelector('input') as HTMLElement);
    };
    for (const [i, v] of ['520', '640', '520', '360'].entries()) {
      await user.type(input(`decl-intr-exterior_image_1_left-${i}`), v);
      await user.tab();
    }
    await waitFor(() => expect(within(drawer).queryByTestId('decl-reason-exterior_image_1_left')).toBeNull());
    await user.click(within(drawer).getByRole('tab', { name: T.tabs.semantics }));
    await user.type(input('decl-pose-frame'), 'panda_link8');
    await user.click(within(drawer).getByRole('tab', { name: T.tabs.tool }));
    await user.click(within(drawer).getByRole('button', { name: T.addTool }));
    expect(await within(drawer).findByTestId('decl-can-generate')).toHaveTextContent(T.trajectory.generate);
    await user.click(within(drawer).getByTestId('decl-save'));
    expect(await screen.findByText(T.saved(1))).toBeInTheDocument();
    expect(db.declarations.get('ds_droid100')?.version).toBe(1);
    await waitFor(() => expect(screen.getByTestId('dataset-declaration-state')).toHaveTextContent('第 1 版'));
  });

  it('a declaration the dataset cannot take is refused with what is wrong', async () => {
    const { user } = renderApp('/datasets/ds_droid100?declaration=1');
    const drawer = await findDrawer('数据集声明 · droid_100');
    await within(drawer).findByTestId('decl-status');
    const doc = { schema_version: 'dataset-declaration/1.0', calibration: { cameras: { 'observation.images.nope': { mount: 'fixed_external' } } } };
    const file = new File([JSON.stringify(doc)], 'bad.json', { type: 'application/json' });
    await user.upload(within(drawer).getByTestId('decl-import') as HTMLInputElement, file);
    expect(await screen.findByText(T.imported('bad.json'))).toBeInTheDocument();
    await user.click(within(drawer).getByTestId('decl-save'));
    expect(await within(drawer).findByTestId('decl-problems')).toHaveTextContent('declaration.calibration.cameras.observation.images.nope');
    expect(db.declarations.get('ds_droid100')).toBeUndefined();
  });
});
