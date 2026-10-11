import React from 'react';
import { t } from '../copy.js';
import { PROVIDERS } from '../api/client.js';
import { storeFromRead, LOCKED_KINDS } from '../status.js';
import { KeyField } from './keyField.jsx';

const mono = "'IBM Plex Mono', ui-monospace, Menlo, monospace";
const note = { fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 12.5, lineHeight: 1.5, color: 'rgba(237,235,230,0.55)', margin: '0 0 8px' };

const DISCLOSURE = [1, 2, 3, 4, 5, 6].map((n) => 'hosted.disclosure.' + n);

function ProviderPicker({ cfg, provider, onProvider }) {
  const shown = PROVIDERS.filter((p) => !cfg || !Array.isArray(cfg.providers) || cfg.providers.includes(p));
  return (
    <div>
      <div style={{ ...note, fontFamily: mono, fontStyle: 'normal', fontSize: 9.5, letterSpacing: '0.1em', textTransform: 'uppercase' }}>{t('keys.provider')}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        {shown.map((p) => (
          <button key={p} className={'co-btn' + (p === provider ? '' : ' quiet')} data-testid={'provider-' + p}
            aria-pressed={p === provider} onClick={() => onProvider(p)}>{t('provider.' + p + '.label')}</button>
        ))}
      </div>
      <div style={note}>{t('provider.hint')}</div>
    </div>
  );
}

// Hosted: the key lives in this tab's memory only and rides on every request.
function HostedKeys({ cfg, provider, onProvider, hostedKey, onHostedKey, onForget }) {
  const [ack, setAck] = React.useState(false);
  return (
    <div>
      <div data-testid="hosted-disclosure" style={{ ...note, fontStyle: 'normal', color: 'rgba(237,235,230,0.75)' }}>
        <div style={{ fontFamily: mono, fontSize: 9.5, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>{t('hosted.disclosure.title')}</div>
        <ul style={{ margin: 0, paddingInlineStart: 18 }}>
          {DISCLOSURE.map((k) => <li key={k} style={{ marginBottom: 4 }}>{t(k)}</li>)}
        </ul>
      </div>
      <ProviderPicker cfg={cfg} provider={provider} onProvider={onProvider} />
      <label style={{ ...note, display: 'flex', gap: 8, alignItems: 'flex-start', fontStyle: 'normal' }}>
        <input type="checkbox" data-testid="apikey-ack" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>{t('keys.ack')}</span>
      </label>
      <KeyField value={hostedKey} onChange={onHostedKey} disabled={!ack} style={{ opacity: ack ? 1 : 0.5 }} />
      <div style={{ ...note, marginTop: 8 }}>{hostedKey ? t('keys.hosted.held') : t('keys.hosted.missing')}</div>
      {hostedKey && <button className="co-btn quiet" data-testid="apikey-forget" onClick={onForget}>{t('keys.hosted.forget')}</button>}
    </div>
  );
}

// Self-host: keys go to the server's encrypted store and are never shown again.
function SelfHostKeys({ client, cfg, provider, onProvider, onDone, onStore }) {
  const [pass, setPass] = React.useState('');
  const [unlocked, setUnlocked] = React.useState(false);
  const [flags, setFlags] = React.useState({});
  const [value, setValue] = React.useState('');
  const [msg, setMsg] = React.useState('');
  const [read, setRead] = React.useState(false);
  const [storeErr, setStoreErr] = React.useState(null);

  const refresh = React.useCallback(async () => {
    const r = await client.keys.status();
    const s = storeFromRead(r);
    setUnlocked(s.unlocked);
    setFlags(s.keys);
    setStoreErr(s.error);
    setRead(true);
  }, [client]);
  React.useEffect(() => { refresh(); }, [refresh]);
  // the page's status icon follows each finished read of the store
  React.useEffect(() => {
    if (read) onStore({ unlocked, keys: flags, error: storeErr });
  }, [read, unlocked, flags, storeErr, onStore]);

  const label = () => t('provider.' + provider + '.label');
  const unlock = async () => {
    const r = await client.keys.unlock(pass);
    setPass('');
    setMsg(r.ok ? '' : t('error.' + r.error.kind));
    if (r.ok) {
      await refresh();
      onDone(t('toast.keys.unlocked'));
    }
  };
  const lock = async () => {
    const r = await client.keys.lock();
    setMsg(r.ok ? '' : t('error.' + r.error.kind));
    if (r.ok) { setUnlocked(false); setFlags({}); onDone(t('toast.keys.locked')); }
  };
  const save = async () => {
    const r = await client.keys.put(provider, value);
    setValue('');
    setMsg(r.ok ? '' : t('error.' + r.error.kind));
    if (r.ok) {
      await refresh();
      onDone(t('toast.keys.saved', { provider: label() }));
    } else if (LOCKED_KINDS.includes(r.error.kind)) await refresh();
  };
  const remove = async () => {
    const r = await client.keys.remove(provider);
    setMsg(r.ok ? '' : t('error.' + r.error.kind));
    await refresh();
    if (r.ok) onDone(t('toast.keys.removed', { provider: label() }));
  };

  return (
    <div>
      <div style={note}>{t('keys.selfhost.intro')}</div>
      {!unlocked ? (
        <div>
          <input className="co-field" type="password" autoComplete="off" data-testid="vault-passphrase"
            aria-label={t('keys.passphrase.label')} placeholder={t('keys.passphrase.label')}
            value={pass} onChange={(e) => setPass(e.target.value)} />
          <div style={{ ...note, marginTop: 8 }}>{t('keys.locked')}</div>
          <button className="co-btn" data-testid="vault-unlock" onClick={unlock}>{t('keys.unlock')}</button>
        </div>
      ) : (
        <div>
          <div style={note}>{t('keys.unlocked')}</div>
          <ProviderPicker cfg={cfg} provider={provider} onProvider={onProvider} />
          <div data-testid="key-status" style={{ ...note, fontFamily: mono, fontStyle: 'normal' }}>
            {t('provider.' + provider + '.label')}: {flags[provider] ? t('keys.status.set') : t('keys.status.unset')}
          </div>
          <KeyField value={value} onChange={setValue} />
          <div style={{ ...note, marginTop: 8 }}>{t('keys.never_shown')}</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="co-btn" data-testid="key-save" disabled={!value} onClick={save}>{t('keys.save')}</button>
            <button className="co-btn quiet" data-testid="key-remove" onClick={remove}>{t('keys.remove')}</button>
            <button className="co-btn quiet" data-testid="vault-lock" onClick={lock}>{t('keys.lock')}</button>
          </div>
        </div>
      )}
      {msg && <div role="alert" data-testid="keys-error" style={{ ...note, color: '#e05252', fontStyle: 'normal', marginTop: 8 }}>{msg}</div>}
    </div>
  );
}

export function KeysSection({ client, cfg, provider, onProvider, hostedKey, onHostedKey, onForget, onDone = () => {}, onStore = () => {} }) {
  // no mode yet: show no key controls rather than guess one
  if (!cfg) return null;
  if (cfg && cfg.mode === 'hosted') {
    return <HostedKeys cfg={cfg} provider={provider} onProvider={onProvider} hostedKey={hostedKey} onHostedKey={onHostedKey} onForget={onForget} />;
  }
  return <SelfHostKeys client={client} cfg={cfg} provider={provider} onProvider={onProvider} onDone={onDone} onStore={onStore} />;
}
