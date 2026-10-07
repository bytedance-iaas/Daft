import { Alert, Button, Checkbox, Drawer, Input, Message, Modal, Select, Space, Spin, Switch, Table } from '@arco-design/web-react';
import { IconArrowDown, IconArrowUp, IconDelete, IconPlus } from '@arco-design/web-react/icon';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { ApiError, errorMessage } from '../../api/errors';
import type { VizDisplay, VizDisplayGroup, VizDisplayLine } from '../../api/types';
import { PageError } from '../../components/PageError';
import { RelTime } from '../../components/RelTime';
import {
  addGroup,
  assignDim,
  dimId,
  drawerConfig,
  drawerState,
  groupOfDim,
  groupsOf,
  moveRow,
  removeGroup,
  setGroup,
  setLine,
  SPEEDS,
  type CameraRow,
  type DrawerState,
} from '../../lib/vizDisplay';
import { zh } from '../../locales/zh';
import { saveVizDisplay, useVizDisplay } from '../visualizer/data';
import './displayConfig.css';

const T = zh.displayCfg;
const ROLES: VizDisplayLine['role'][] = ['state', 'action', 'other'];

/**
 * 「展示配置」 of a registered dataset (design doc 21 §6.5): the cameras' order, names and which ones
 * the layout templates leave out; the curve groups made of the dataset's dimensions (LeRobot /
 * Lance); the subtitle track; default speed and looping. 保存 replaces the configuration (the
 * default layout saved from the player is kept), 恢复默认 removes all of it.
 */
export function DisplayConfigDrawer({ dataset, onClose }: { dataset: { id: string; name: string } | null; onClose: () => void }) {
  return dataset ? <Opened key={dataset.id} dataset={dataset} onClose={onClose} /> : null;
}

function Opened({ dataset, onClose }: { dataset: { id: string; name: string }; onClose: () => void }) {
  const qc = useQueryClient();
  const doc = useVizDisplay(dataset.id);
  const d = doc.data;
  const [st, setSt] = useState<DrawerState | null>(null);
  useEffect(() => {
    if (d && !st) setSt(drawerState(d));
  }, [d, st]);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<{ field: string; problem: string }[]>([]);
  const next = d && st ? drawerConfig(d, st) : null;
  const unchanged = !d || !st || JSON.stringify(next) === JSON.stringify(d.config ?? null);

  const write = async (config: typeof next, done: string) => {
    setBusy(true);
    setProblems([]);
    try {
      await saveVizDisplay(qc, dataset.id, config);
      Message.success(done);
      onClose();
    } catch (e) {
      const details = e instanceof ApiError ? (e.details as { errors?: { field: string; problem: string }[] } | undefined) : undefined;
      setProblems(details?.errors ?? []);
      Message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const restore = () =>
    Modal.confirm({
      title: T.restoreTitle,
      content: T.restoreContent,
      okText: T.restore,
      cancelText: zh.common.cancel,
      okButtonProps: { status: 'danger' },
      onOk: () => write(null, T.restored),
    });

  return (
    <Drawer
      width={900}
      title={T.title(dataset.name)}
      visible
      onCancel={onClose}
      footer={
        <Space>
          <Button status="danger" disabled={!d?.config || busy} onClick={restore}>
            {T.restore}
          </Button>
          <Button onClick={onClose}>{zh.common.cancel}</Button>
          <Button type="primary" loading={busy} disabled={unchanged} title={unchanged ? T.unchanged : undefined} onClick={() => void write(next, T.saved)}>
            {T.save}
          </Button>
        </Space>
      }
    >
      {doc.isError && !d ? (
        <PageError error={doc.error} onRetry={() => void doc.refetch()} />
      ) : !d || !st ? (
        <Spin style={{ display: 'block', margin: '60px auto' }} />
      ) : (
        <div className="dispcfg" data-testid="display-drawer">
          <div className="dispcfg-current" data-testid="display-current">
            {d.config ? (
              <span>
                {T.current(d.version)}
                {d.updated_at ? (
                  <span className="muted">
                    {' · '}
                    <RelTime ms={d.updated_at} />
                  </span>
                ) : null}
              </span>
            ) : (
              <span className="muted">{T.none}</span>
            )}
          </div>
          {problems.length ? (
            <Alert
              type="error"
              style={{ marginBottom: 12 }}
              data-testid="display-problems"
              content={
                <div>
                  {T.problems(problems.length)}
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {problems.slice(0, 10).map((p, i) => (
                      <li key={i}>
                        <span className="mono">{p.field}</span>：{p.problem}
                      </li>
                    ))}
                  </ul>
                </div>
              }
            />
          ) : null}
          <Cameras rows={st.cameras} onChange={(cameras) => setSt({ ...st, cameras })} />
          <Groups doc={d} groups={st.groups} onChange={(groups) => setSt({ ...st, groups })} />
          <h4 className="dispcfg-h">{T.track}</h4>
          {d.defaults.tracks.length ? (
            <Select
              size="small"
              style={{ width: 320 }}
              value={st.track ?? ''}
              aria-label={T.track}
              options={[{ value: '', label: T.trackAuto }, ...d.defaults.tracks.map((t) => ({ value: t.key, label: `${t.name}（${t.key}）` }))]}
              onChange={(v: string) => setSt({ ...st, track: v || null })}
            />
          ) : (
            <div className="muted">{T.tracksNone}</div>
          )}
          <h4 className="dispcfg-h">{T.playback}</h4>
          <Space size={16}>
            <Space size={6}>
              <span>{T.speed}</span>
              <Select size="small" style={{ width: 84 }} value={st.speed} aria-label={T.speed} onChange={(v: number) => setSt({ ...st, speed: v })}>
                {SPEEDS.map((v) => (
                  <Select.Option key={v} value={v}>
                    {`${v}x`}
                  </Select.Option>
                ))}
              </Select>
            </Space>
            <Space size={6}>
              <span>{T.loop}</span>
              <Switch size="small" checked={st.loop} aria-label={T.loop} onChange={(v: boolean) => setSt({ ...st, loop: v })} />
            </Space>
          </Space>
        </div>
      )}
    </Drawer>
  );
}

/** 相机: one row a camera - its key, a display name, whether the templates leave it out; up / down. */
function Cameras({ rows, onChange }: { rows: CameraRow[]; onChange: (rows: CameraRow[]) => void }) {
  const set = (i: number, patch: Partial<CameraRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <>
      <h4 className="dispcfg-h">{T.cameras}</h4>
      {rows.length ? (
        <Table
          size="small"
          rowKey="key"
          pagination={false}
          border={false}
          data={rows}
          data-testid="display-cameras"
          columns={[
            {
              title: T.colCamera,
              dataIndex: 'key',
              render: (_: unknown, r: CameraRow) => (
                <div>
                  <div className="mono">{r.key}</div>
                  <div className="muted dispcfg-small mono">{r.source}</div>
                </div>
              ),
            },
            {
              title: T.colName,
              dataIndex: 'name',
              width: 220,
              render: (_: unknown, r: CameraRow, i: number) => (
                <Input size="small" value={r.name} placeholder={r.own} maxLength={64} aria-label={T.nameAria(r.key)} onChange={(v) => set(i, { name: v })} />
              ),
            },
            {
              title: <span title={T.hiddenTip}>{T.colHidden}</span>,
              dataIndex: 'hidden',
              width: 90,
              render: (_: unknown, r: CameraRow, i: number) => <Checkbox checked={r.hidden} aria-label={T.hiddenAria(r.key)} onChange={(v: boolean) => set(i, { hidden: v })} />,
            },
            {
              title: '',
              dataIndex: 'move',
              width: 80,
              render: (_: unknown, r: CameraRow, i: number) => (
                <Space size={2}>
                  <Button size="mini" type="text" icon={<IconArrowUp />} disabled={i === 0} aria-label={`${T.up} ${r.key}`} title={T.up} onClick={() => onChange(moveRow(rows, i, -1))} />
                  <Button size="mini" type="text" icon={<IconArrowDown />} disabled={i === rows.length - 1} aria-label={`${T.down} ${r.key}`} title={T.down} onClick={() => onChange(moveRow(rows, i, 1))} />
                </Space>
              ),
            },
          ]}
        />
      ) : (
        <div className="muted">{T.camerasNone}</div>
      )}
    </>
  );
}

/** 曲线分组 (LeRobot / Lance): the groups, then every dimension with its name, role and group. */
function Groups({ doc, groups, onChange }: { doc: VizDisplay; groups: VizDisplayGroup[]; onChange: (g: VizDisplayGroup[]) => void }) {
  const dims = doc.defaults.dimensions;
  const options = useMemo(() => [...groups.map((g) => ({ value: g.key, label: g.name })), { value: '', label: T.notDrawn }], [groups]);
  if (!doc.defaults.groups_editable) {
    return (
      <>
        <h4 className="dispcfg-h">{T.groups}</h4>
        <div className="muted">{T.groupsMcap}</div>
      </>
    );
  }
  if (!dims.length) {
    return (
      <>
        <h4 className="dispcfg-h">{T.groups}</h4>
        <div className="muted">{T.groupsNone}</div>
      </>
    );
  }
  return (
    <>
      <div className="dispcfg-h dispcfg-row">
        <h4>{T.groups}</h4>
        <Space size={8}>
          <Button size="mini" icon={<IconPlus />} onClick={() => onChange(addGroup(groups, T.newGroup))}>
            {T.addGroup}
          </Button>
          <Button size="mini" onClick={() => onChange(groupsOf(null, doc.defaults))}>
            {T.autoGroups}
          </Button>
        </Space>
      </div>
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        border={false}
        data={groups}
        data-testid="display-groups"
        columns={[
          {
            title: T.colGroup,
            dataIndex: 'name',
            render: (_: unknown, g: VizDisplayGroup) => (
              <div>
                <Input size="small" value={g.name} maxLength={128} aria-label={T.groupAria(g.key)} onChange={(v) => onChange(setGroup(groups, g.key, { name: v }))} />
                <div className="muted dispcfg-small mono">{g.key}</div>
              </div>
            ),
          },
          {
            title: T.colUnit,
            dataIndex: 'unit',
            width: 110,
            render: (_: unknown, g: VizDisplayGroup) => (
              <Input size="small" value={g.unit ?? ''} maxLength={32} aria-label={T.unitAria(g.key)} onChange={(v) => onChange(setGroup(groups, g.key, { unit: v || null }))} />
            ),
          },
          {
            title: T.colSmart,
            dataIndex: 'smart',
            width: 96,
            render: (_: unknown, g: VizDisplayGroup) => <Checkbox checked={g.smart} onChange={(v: boolean) => onChange(setGroup(groups, g.key, { smart: v }))} />,
          },
          {
            title: T.colLines,
            dataIndex: 'lines',
            width: 150,
            render: (_: unknown, g: VizDisplayGroup) => (g.lines.length ? <span>{g.lines.length}</span> : <span className="dispcfg-warn">{T.emptyGroup}</span>),
          },
          {
            title: '',
            dataIndex: 'remove',
            width: 48,
            render: (_: unknown, g: VizDisplayGroup) => (
              <Button size="mini" type="text" status="danger" icon={<IconDelete />} aria-label={`${T.removeGroup} ${g.key}`} title={T.removeGroup} onClick={() => onChange(removeGroup(groups, g.key))} />
            ),
          },
        ]}
      />
      <h4 className="dispcfg-h">{T.dims}</h4>
      <Table
        size="small"
        rowKey={(l: VizDisplayLine) => dimId(l)}
        pagination={false}
        border={false}
        scroll={{ y: 360 }}
        data={dims}
        data-testid="display-dims"
        columns={[
          { title: T.colFeature, dataIndex: 'source', render: (_: unknown, l: VizDisplayLine) => <span className="mono">{l.source}</span> },
          { title: T.colDim, dataIndex: 'dim', width: 60 },
          {
            title: T.colLine,
            dataIndex: 'name',
            width: 170,
            render: (_: unknown, l: VizDisplayLine) => {
              const g = groupOfDim(groups, dimId(l));
              const line = g?.lines.find((x) => dimId(x) === dimId(l));
              return line ? (
                <Input size="small" value={line.name} maxLength={128} aria-label={T.lineAria(dimId(l))} onChange={(v) => onChange(setLine(groups, dimId(l), { name: v }))} />
              ) : (
                <span className="muted">{l.name}</span>
              );
            },
          },
          {
            title: T.colRole,
            dataIndex: 'role',
            width: 128,
            render: (_: unknown, l: VizDisplayLine) => {
              const line = groupOfDim(groups, dimId(l))?.lines.find((x) => dimId(x) === dimId(l));
              return line ? (
                <Select
                  size="mini"
                  value={line.role}
                  aria-label={T.roleAria(dimId(l))}
                  options={ROLES.map((r) => ({ value: r, label: T.roles[r] }))}
                  onChange={(v: VizDisplayLine['role']) => onChange(setLine(groups, dimId(l), { role: v }))}
                />
              ) : (
                <span className="muted">{T.roles[l.role]}</span>
              );
            },
          },
          {
            title: T.colIn,
            dataIndex: 'group',
            width: 190,
            render: (_: unknown, l: VizDisplayLine) => (
              <Select
                size="mini"
                value={groupOfDim(groups, dimId(l))?.key ?? ''}
                aria-label={T.inAria(dimId(l))}
                options={options}
                onChange={(v: string) => onChange(assignDim(groups, l, v || null))}
              />
            ),
          },
        ]}
      />
    </>
  );
}
