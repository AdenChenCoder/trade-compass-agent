import { useState, useSyncExternalStore } from 'react';
import { appearances, getAppearance, setAppearance, subscribeAppearance } from '../../shared/appearance';
import { Icon } from './Icon';

export function AppearanceSettings() {
  const selected = useSyncExternalStore(subscribeAppearance, getAppearance);
  const [saveFailed, setSaveFailed] = useState(false);
  return <section className="connection-card appearance-settings" aria-label="外观">
    <div className="appearance-heading"><h3>配色风格</h3><button className="appearance-reset" disabled={selected === 'classic'} onClick={() => setSaveFailed(!setAppearance('classic'))}>{selected === 'classic' ? '使用默认配色' : '恢复默认'}</button></div>
    <fieldset className="appearance-options"><legend className="sr-only">配色风格</legend>
      {appearances.map(theme => <label className="appearance-option" key={theme.id}>
        <input className="sr-only" type="radio" name="appearance" value={theme.id} checked={selected === theme.id} onChange={() => setSaveFailed(!setAppearance(theme.id))} />
        <span className="appearance-preview" data-theme={theme.id} aria-hidden="true"><span className="appearance-preview-logo" /><span className="appearance-preview-line" /><span className="appearance-preview-action" /><span className="appearance-check"><Icon name="check" /></span></span>
        <span>{theme.name}</span>
      </label>)}
    </fieldset>
    <p className="appearance-hint">仅保存在当前设备。</p>
    {saveFailed ? <p className="field-error" role="alert">配色已应用，但无法保存。下次打开时可能恢复默认配色。</p> : null}
  </section>;
}
