import { Button, Checkbox, Popover, Radio } from '@arco-design/web-react';
import { IconEye } from '@arco-design/web-react/icon';
import type { EefOverlayCamera } from '../../api/types';
import { DEFAULT_CHOICE, layerEntries, layerOn, toggleLayer, type LayerEntry, type OverlayChoice, type OverlayMode } from '../../lib/eefOverlay';
import { zh } from '../../locales/zh';

const O = () => zh.viz.overlay;

/** The layer list's sections: the marks first, the measured ones last; the dataset record's own (dashed) after. */
const GROUP_ORDER: readonly LayerEntry['group'][] = ['declared', 'axes', 'trail_past', 'trail_future', 'observed', 'residual', 'record'];

/** A camera nothing is drawn on, by its player name, and why (a registration's overlay, C4 5.3.0). */
export interface UnavailableCamera {
  name: string;
  reason: string | null;
}

/**
 * The 「叠加」 menu (design doc 22 §3.3): 原图 / 叠加 / 只看观测, the two presets, the hands and the layers the
 * cameras declare (ticking one makes the choice the viewer's own), and the labels. On a registration's page
 * (design doc 25 §5.1, `dataset`) nothing was shown to a model - no such preset - and the cameras it cannot
 * draw on are listed greyed with their reason; a wrist camera's marks come with a note (§5.4).
 */
export function OverlayMenu({
  cameras,
  choice,
  onChange,
  dataset = false,
  unavailable = [],
}: {
  cameras: readonly EefOverlayCamera[];
  choice: OverlayChoice;
  onChange: (c: OverlayChoice) => void;
  dataset?: boolean;
  unavailable?: readonly UnavailableCamera[];
}) {
  const entries = layerEntries(cameras);
  const layers = cameras.flatMap((c) => c.layers);
  const hands = [...new Map(cameras.flatMap((c) => c.hands).map((h) => [h.id, h])).values()];
  const groups = GROUP_ORDER.filter((g) => entries.some((e) => e.group === g));
  // ticked: drawn under the choice (the hands aside)
  const on = (e: LayerEntry) => {
    const l = layers.find((x) => x.id === e.id);
    return !!l && layerOn({ ...choice, hands: {} }, l);
  };
  const content = (
    <div className="vz-ovm" data-testid="vz-overlay-menu">
      <Radio.Group type="button" size="mini" value={choice.mode} onChange={(v: OverlayMode) => onChange({ ...choice, mode: v })}>
        {(['off', 'on', 'observed'] as const).map((m) => (
          <Radio key={m} value={m} disabled={m === 'observed' && !entries.some((e) => e.group === 'observed')}>
            {O().modes[m]}
          </Radio>
        ))}
      </Radio.Group>
      <div className="row">
        <span className="k">{O().preset}</span>
        <Button size="mini" type={choice.preset === 'default' ? 'primary' : 'secondary'} onClick={() => onChange({ ...choice, mode: 'on', preset: 'default', layers: {} })}>
          {O().presets.default}
        </Button>
        {!dataset ? (
          <Button size="mini" type={choice.preset === 'model' ? 'primary' : 'secondary'} title={O().presets.modelTitle} onClick={() => onChange({ ...choice, mode: 'on', preset: 'model', maxGapMs: null })}>
            {O().presets.model}
          </Button>
        ) : null}
      </div>
      {hands.length > 1 ? (
        <div className="row">
          <span className="k">{O().hands}</span>
          {hands.map((h) => (
            <Checkbox key={h.id} checked={choice.hands[h.id] !== false} onChange={(v: boolean) => onChange({ ...choice, hands: { ...choice.hands, [h.id]: v } })}>
              <span className="sw" style={{ background: h.color }} />
              {h.title}
            </Checkbox>
          ))}
        </div>
      ) : null}
      {groups.map((g) => (
        <div key={g} className="grp">
          <div className="k">{O().groups[g] ?? g}</div>
          {entries
            .filter((e) => e.group === g)
            .map((e) => (
              <Checkbox key={e.id} checked={on(e)} disabled={choice.mode === 'observed' && g !== 'observed'} onChange={(v: boolean) => onChange(toggleLayer(choice, layers, e.id, v))} data-testid={`vz-layer-${e.id}`}>
                <span className="sw" style={{ background: e.color }} />
                {e.title}
              </Checkbox>
            ))}
        </div>
      ))}
      {unavailable.length ? (
        <div className="grp" data-testid="vz-overlay-unavailable">
          <div className="k">{O().unavailable}</div>
          {unavailable.map((u) => (
            <div key={u.name} className="off">
              <Checkbox checked={false} disabled>
                {u.name}
              </Checkbox>
              <span className="muted">{O().reasons[u.reason ?? ''] ?? u.reason ?? ''}</span>
            </div>
          ))}
        </div>
      ) : null}
      {cameras.some((c) => c.mount === 'wrist') ? (
        <div className="note" data-testid="vz-overlay-wrist-note">
          {O().wristNote}
        </div>
      ) : null}
      <div className="row">
        <Checkbox checked={choice.labels} onChange={(v: boolean) => onChange({ ...choice, labels: v })}>
          {O().labels}
        </Checkbox>
        <span className="spacer" />
        <Button size="mini" type="text" onClick={() => onChange(DEFAULT_CHOICE)}>
          {O().reset}
        </Button>
      </div>
    </div>
  );
  return (
    <Popover trigger="click" position="br" content={content} unmountOnExit={false}>
      <Button size="small" icon={<IconEye />} className={choice.mode === 'off' ? '' : 'on'} title={O().menuTitle} data-testid="vz-overlay">
        {O().menu}
      </Button>
    </Popover>
  );
}
