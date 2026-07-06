import { memo, useEffect, useState } from 'react';

import {
  createTotpSecret,
  disableAccountTwoFactor,
  enableAccountTwoFactor,
  fetchPlexLibraries,
  sendTestNotification,
  testPlexConnection,
  testProwlarrConnection,
  testQBittorrentConnection,
  testSabnzbdConnection,
  updateAccountSettings,
  updateNotificationSettings,
  updatePlexSettings,
  updateProwlarrSettings,
  updateQBittorrentSettings,
  updateSabnzbdSettings,
  updateSettings,
} from '../api';
import { SETTINGS_SECTIONS_DEFAULT } from '../lib/appUtils';
import {
  Btn,
  FormField,
  SectionCard,
  SectionTitle,
  SelectInput,
  SettingsSectionCard,
  TextInput,
  Toggle,
} from './ui';

function SettingsPage({
  settings,
  setSettings,
  accountSettings,
  setAccountSettings,
  notificationSettings,
  setNotificationSettings,
  plexSettings,
  setPlexSettings,
  plexLibraries,
  setPlexLibraries,
  prowlarrSettings,
  setProwlarrSettings,
  qbtSettings,
  setQbtSettings,
  sabSettings,
  setSabSettings,
  setAuthStatus,
  duplicateCleanupRunning,
  pushToast,
  onShowQrCode,
  onRecoveryRun,
  onCleanupRun,
  onOptimizedCleanupRun,
  onDuplicateOptimizedCleanupRun,
}) {
  const [settingsSectionsOpen, setSettingsSectionsOpen] = useState(SETTINGS_SECTIONS_DEFAULT);
  const [savingSettings, setSavingSettings] = useState(false);
  const [accountForm, setAccountForm] = useState({
    username: '',
    currentPassword: '',
    newPassword: '',
    confirmNewPassword: '',
  });
  const [savingAccountSettings, setSavingAccountSettings] = useState(false);
  const [enablingTwoFactor, setEnablingTwoFactor] = useState(false);
  const [disablingTwoFactor, setDisablingTwoFactor] = useState(false);
  const [generatingAccountTotpSecret, setGeneratingAccountTotpSecret] = useState(false);
  const [accountTwoFactorDraft, setAccountTwoFactorDraft] = useState({
    totpSecret: '',
    totpUri: '',
    totpCode: '',
    currentPassword: '',
  });
  const [accountDisableTwoFactorDraft, setAccountDisableTwoFactorDraft] = useState({
    currentPassword: '',
    totpCode: '',
  });
  const [loadingPlexLibraries, setLoadingPlexLibraries] = useState(false);
  const [savingPlexSettings, setSavingPlexSettings] = useState(false);
  const [testingPlexConnection, setTestingPlexConnection] = useState(false);
  const [savingProwlarrSettings, setSavingProwlarrSettings] = useState(false);
  const [testingProwlarrConnection, setTestingProwlarrConnection] = useState(false);
  const [savingQbtSettings, setSavingQbtSettings] = useState(false);
  const [testingQbtConnection, setTestingQbtConnection] = useState(false);
  const [savingSabSettings, setSavingSabSettings] = useState(false);
  const [testingSabConnection, setTestingSabConnection] = useState(false);

  useEffect(() => {
    if (!accountSettings) return;
    setAccountForm((prev) => ({
      ...prev,
      username: accountSettings.username ?? '',
    }));
  }, [accountSettings?.username]);

  function toggleSettingsSection(sectionKey) {
    setSettingsSectionsOpen((prev) => ({ ...prev, [sectionKey]: !prev[sectionKey] }));
  }

  async function saveSettings() {
    if (!settings) return;
    setSavingSettings(true);
    try {
      const updated = await updateSettings(settings);
      setSettings(updated);
      pushToast('Settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Failed to save settings.', 'error');
    } finally {
      setSavingSettings(false);
    }
  }

  async function saveAccountSettings() {
    if (!accountSettings) return;
    const username = accountForm.username.trim();
    const currentPassword = accountForm.currentPassword;
    const newPassword = accountForm.newPassword;
    const confirm = accountForm.confirmNewPassword;

    if (!currentPassword) {
      pushToast('Current password is required.', 'error');
      return;
    }
    if (!username) {
      pushToast('Username is required.', 'error');
      return;
    }
    if (newPassword && newPassword !== confirm) {
      pushToast('New password confirmation does not match.', 'error');
      return;
    }
    if (newPassword && newPassword.length < 12) {
      pushToast('New password must be at least 12 characters.', 'error');
      return;
    }

    const payload = { current_password: currentPassword };
    if (username !== accountSettings.username) payload.username = username;
    if (newPassword) payload.new_password = newPassword;

    setSavingAccountSettings(true);
    try {
      const updated = await updateAccountSettings(payload);
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, username: updated.username, two_factor_enabled: updated.two_factor_enabled }));
      setAccountForm((prev) => ({ ...prev, currentPassword: '', newPassword: '', confirmNewPassword: '' }));
      pushToast('Account settings updated.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to update account settings.', 'error');
    } finally {
      setSavingAccountSettings(false);
    }
  }

  async function generateAccountTotpSecret() {
    if (!accountSettings?.username) return;
    setGeneratingAccountTotpSecret(true);
    try {
      const payload = await createTotpSecret(accountSettings.username);
      setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: payload.secret, totpUri: payload.otpauth_url || '' }));
      pushToast('Generated 2FA secret.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to generate 2FA secret.', 'error');
    } finally {
      setGeneratingAccountTotpSecret(false);
    }
  }

  async function openAccountQrCode() {
    if (!accountSettings?.username) return;
    setGeneratingAccountTotpSecret(true);
    try {
      const shouldGenerate = !accountTwoFactorDraft.totpSecret.trim() || !accountTwoFactorDraft.totpUri.trim();
      let secret = accountTwoFactorDraft.totpSecret.trim();
      let otpauthUrl = accountTwoFactorDraft.totpUri.trim();
      if (shouldGenerate) {
        const payload = await createTotpSecret(accountSettings.username);
        secret = payload.secret;
        otpauthUrl = payload.otpauth_url || '';
        setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: secret, totpUri: otpauthUrl }));
      }
      onShowQrCode({
        title: 'Use QR Code for 2FA',
        subtitle: 'Scan this code in your authenticator app, then enter the current code and your password to enable 2FA.',
        secret,
        otpauthUrl,
      });
    } catch (err) {
      pushToast(err.message || 'Failed to generate 2FA QR code.', 'error');
    } finally {
      setGeneratingAccountTotpSecret(false);
    }
  }

  async function enableTwoFactorForAccount() {
    if (!accountTwoFactorDraft.currentPassword) {
      pushToast('Current password is required to enable 2FA.', 'error');
      return;
    }
    if (!accountTwoFactorDraft.totpSecret) {
      pushToast('Generate a TOTP secret first.', 'error');
      return;
    }
    if (!accountTwoFactorDraft.totpCode) {
      pushToast('Enter a valid authenticator code.', 'error');
      return;
    }

    setEnablingTwoFactor(true);
    try {
      const updated = await enableAccountTwoFactor({
        current_password: accountTwoFactorDraft.currentPassword,
        totp_secret: accountTwoFactorDraft.totpSecret.trim(),
        totp_code: accountTwoFactorDraft.totpCode.trim(),
      });
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, two_factor_enabled: true }));
      setAccountTwoFactorDraft({ totpSecret: '', totpUri: '', totpCode: '', currentPassword: '' });
      pushToast('Dual-factor authentication enabled.', 'success', { durationMs: 9000 });
    } catch (err) {
      pushToast(err.message || 'Failed to enable 2FA.', 'error');
    } finally {
      setEnablingTwoFactor(false);
    }
  }

  async function disableTwoFactorForAccount() {
    if (!accountDisableTwoFactorDraft.currentPassword) {
      pushToast('Current password is required to disable 2FA.', 'error');
      return;
    }
    if (!accountDisableTwoFactorDraft.totpCode) {
      pushToast('Enter your authenticator code to disable 2FA.', 'error');
      return;
    }

    setDisablingTwoFactor(true);
    try {
      const updated = await disableAccountTwoFactor({
        current_password: accountDisableTwoFactorDraft.currentPassword,
        totp_code: accountDisableTwoFactorDraft.totpCode.trim(),
      });
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, two_factor_enabled: false }));
      setAccountDisableTwoFactorDraft({ currentPassword: '', totpCode: '' });
      pushToast('Dual-factor authentication disabled.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to disable 2FA.', 'error');
    } finally {
      setDisablingTwoFactor(false);
    }
  }

  async function saveNotificationSettings() {
    if (!notificationSettings) return;
    setSavingSettings(true);
    try {
      const updated = await updateNotificationSettings(notificationSettings);
      setNotificationSettings(updated);
      pushToast('Notification settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not save notification settings.', 'error');
    } finally {
      setSavingSettings(false);
    }
  }

  async function sendNotificationTest() {
    try {
      await sendTestNotification();
      pushToast('Queued a test notification email.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not queue test email.', 'error');
    }
  }

  async function savePlexSettings() {
    if (!plexSettings) return;
    setSavingPlexSettings(true);
    try {
      const updated = await updatePlexSettings(plexSettings);
      setPlexSettings(updated);
      pushToast('Plex settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not save Plex settings.', 'error');
    } finally {
      setSavingPlexSettings(false);
    }
  }

  async function loadPlexLibraries() {
    setLoadingPlexLibraries(true);
    try {
      const sections = await fetchPlexLibraries();
      setPlexLibraries(sections ?? []);
    } catch (fetchError) {
      pushToast(fetchError.message || 'Could not fetch Plex library sections.', 'error');
    } finally {
      setLoadingPlexLibraries(false);
    }
  }

  async function handleTestPlexConnection() {
    setTestingPlexConnection(true);
    try {
      const result = await testPlexConnection();
      if (result?.success) {
        pushToast('Plex connection successful.', 'success');
        await loadPlexLibraries();
      } else {
        pushToast(result?.error || 'Plex connection failed.', 'error');
      }
    } catch (testError) {
      pushToast(testError.message || 'Plex connection test failed.', 'error');
    } finally {
      setTestingPlexConnection(false);
    }
  }

  async function saveProwlarrSettings() {
    if (!prowlarrSettings) return;
    setSavingProwlarrSettings(true);
    try {
      const updated = await updateProwlarrSettings(prowlarrSettings);
      setProwlarrSettings(updated);
      pushToast('Prowlarr settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save Prowlarr settings.', 'error');
    } finally {
      setSavingProwlarrSettings(false);
    }
  }

  async function handleTestProwlarrConnection() {
    setTestingProwlarrConnection(true);
    try {
      const result = await testProwlarrConnection();
      if (result?.success) {
        pushToast(`Prowlarr connected. Found ${result.indexer_count ?? 0} indexer(s).`, 'success');
      } else {
        pushToast(result?.error || 'Prowlarr connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'Prowlarr connection test failed.', 'error');
    } finally {
      setTestingProwlarrConnection(false);
    }
  }

  async function saveQbtSettings() {
    if (!qbtSettings) return;
    setSavingQbtSettings(true);
    try {
      const updated = await updateQBittorrentSettings(qbtSettings);
      setQbtSettings(updated);
      pushToast('qBittorrent settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save qBittorrent settings.', 'error');
    } finally {
      setSavingQbtSettings(false);
    }
  }

  async function handleTestQbtConnection() {
    setTestingQbtConnection(true);
    try {
      const result = await testQBittorrentConnection();
      if (result?.success) {
        pushToast(`qBittorrent connected. Version: ${result.version ?? 'unknown'}.`, 'success');
      } else {
        pushToast(result?.error || 'qBittorrent connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'qBittorrent connection test failed.', 'error');
    } finally {
      setTestingQbtConnection(false);
    }
  }

  async function saveSabSettings() {
    if (!sabSettings) return;
    setSavingSabSettings(true);
    try {
      const updated = await updateSabnzbdSettings(sabSettings);
      setSabSettings(updated);
      pushToast('SABnzbd settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save SABnzbd settings.', 'error');
    } finally {
      setSavingSabSettings(false);
    }
  }

  async function handleTestSabConnection() {
    setTestingSabConnection(true);
    try {
      const result = await testSabnzbdConnection();
      if (result?.success) {
        pushToast(`SABnzbd connected. Version: ${result.version ?? 'unknown'}.`, 'success');
      } else {
        pushToast(result?.error || 'SABnzbd connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'SABnzbd connection test failed.', 'error');
    } finally {
      setTestingSabConnection(false);
    }
  }

  if (!(settings && accountSettings && notificationSettings && plexSettings)) {
    return (
      <section className="animate-fade-in">
        <SectionCard>
          <SectionTitle>Settings</SectionTitle>
          <p className="text-sm text-slate-300">Loading settings...</p>
        </SectionCard>
      </section>
    );
  }

  return (
    <section className="animate-fade-in space-y-5">
      <SettingsSectionCard title="Account Settings" open={settingsSectionsOpen.account} onToggle={() => toggleSettingsSection('account')}>
        <div className="grid gap-4 md:grid-cols-2">
          <FormField label="Username">
            <TextInput
              type="text"
              value={accountForm.username}
              onChange={(e) => setAccountForm((prev) => ({ ...prev, username: e.target.value }))}
              autoComplete="username"
            />
          </FormField>
          <FormField label="Current Password" hint="Required to change username/password and enable 2FA.">
            <TextInput
              type="password"
              value={accountForm.currentPassword}
              onChange={(e) => setAccountForm((prev) => ({ ...prev, currentPassword: e.target.value }))}
              autoComplete="current-password"
            />
          </FormField>
          <FormField label="New Password" hint="Optional. Minimum 12 characters." span2>
            <TextInput
              type="password"
              value={accountForm.newPassword}
              onChange={(e) => setAccountForm((prev) => ({ ...prev, newPassword: e.target.value }))}
              autoComplete="new-password"
            />
          </FormField>
          <FormField label="Confirm New Password" span2>
            <TextInput
              type="password"
              value={accountForm.confirmNewPassword}
              onChange={(e) => setAccountForm((prev) => ({ ...prev, confirmNewPassword: e.target.value }))}
              autoComplete="new-password"
            />
          </FormField>
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <Btn variant="violet" disabled={savingAccountSettings} onClick={saveAccountSettings}>
            {savingAccountSettings ? 'Saving…' : 'Save Account'}
          </Btn>
          <span className="rounded-full border border-slate-700 bg-slate-900/80 px-3 py-2 text-xs text-slate-300">
            2FA: {accountSettings.two_factor_enabled ? 'Enabled' : 'Disabled'}
          </span>
        </div>

        {!accountSettings.two_factor_enabled && (
          <div className="mt-5 space-y-3 rounded-xl border border-slate-700/80 bg-slate-950/50 p-4">
            <p className="text-sm font-medium text-slate-200">Enable Dual-Factor Authentication</p>
            <p className="text-xs text-slate-500">Generate a TOTP secret, add it to your authenticator app, then verify with a live code.</p>
            <div className="flex flex-wrap gap-3">
              <Btn variant="primary" disabled={generatingAccountTotpSecret} onClick={openAccountQrCode}>
                {generatingAccountTotpSecret ? 'Generating…' : 'Use QR Code'}
              </Btn>
              <Btn variant="secondary" disabled={generatingAccountTotpSecret} onClick={generateAccountTotpSecret}>
                {generatingAccountTotpSecret ? 'Generating…' : 'Generate 2FA Secret'}
              </Btn>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <FormField label="TOTP Secret" span2>
                <TextInput
                  type="text"
                  value={accountTwoFactorDraft.totpSecret}
                  onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: e.target.value, totpUri: '' }))}
                />
              </FormField>
              <FormField label="Authenticator Code">
                <TextInput
                  type="text"
                  inputMode="numeric"
                  value={accountTwoFactorDraft.totpCode}
                  onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, totpCode: e.target.value }))}
                />
              </FormField>
              <FormField label="Current Password">
                <TextInput
                  type="password"
                  autoComplete="current-password"
                  value={accountTwoFactorDraft.currentPassword}
                  onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, currentPassword: e.target.value }))}
                />
              </FormField>
            </div>
            <div>
              <Btn variant="primary" disabled={enablingTwoFactor} onClick={enableTwoFactorForAccount}>
                {enablingTwoFactor ? 'Enabling…' : 'Enable 2FA'}
              </Btn>
            </div>
          </div>
        )}

        {accountSettings.two_factor_enabled && (
          <div className="mt-5 space-y-3 rounded-xl border border-red-900/40 bg-red-950/10 p-4">
            <p className="text-sm font-medium text-red-200">Disable Dual-Factor Authentication</p>
            <p className="text-xs text-red-300/80">For security, confirm with your current password and a valid authenticator code.</p>
            <div className="grid gap-4 md:grid-cols-2">
              <FormField label="Current Password">
                <TextInput
                  type="password"
                  autoComplete="current-password"
                  value={accountDisableTwoFactorDraft.currentPassword}
                  onChange={(e) => setAccountDisableTwoFactorDraft((prev) => ({ ...prev, currentPassword: e.target.value }))}
                />
              </FormField>
              <FormField label="Authenticator Code">
                <TextInput
                  type="text"
                  inputMode="numeric"
                  value={accountDisableTwoFactorDraft.totpCode}
                  onChange={(e) => setAccountDisableTwoFactorDraft((prev) => ({ ...prev, totpCode: e.target.value }))}
                />
              </FormField>
            </div>
            <div>
              <Btn variant="danger" disabled={disablingTwoFactor} onClick={disableTwoFactorForAccount}>
                {disablingTwoFactor ? 'Disabling…' : 'Disable 2FA'}
              </Btn>
            </div>
          </div>
        )}
      </SettingsSectionCard>

      <SettingsSectionCard title="General Settings" open={settingsSectionsOpen.general} onToggle={() => toggleSettingsSection('general')}>
        <div className="grid gap-4 md:grid-cols-2">
          <FormField label="History Retention (Days)" hint="How long to keep completed job history.">
            <TextInput type="number" min={1} value={settings.history_retention_days} onChange={(e) => setSettings((prev) => ({ ...prev, history_retention_days: Number(e.target.value) }))} />
          </FormField>

          <FormField label="Discovery Interval (Minutes)" hint="How often to scan libraries when using interval discovery.">
            <TextInput type="number" min={1} value={settings.discovery_interval_minutes} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_interval_minutes: Number(e.target.value) }))} />
          </FormField>

          <FormField label="Discovery Method" hint="How Optimizarr discovers new media files. Watcher mode avoids repeated full-library rescans.">
            <SelectInput value={settings.discovery_method} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_method: e.target.value }))}>
              <option value="interval">On Interval</option>
              <option value="watcher">Watcher</option>
            </SelectInput>
          </FormField>

          <FormField label="Workspace Root" hint="Temporary directory used during encoding.">
            <TextInput type="text" value={settings.workspace_root} onChange={(e) => setSettings((prev) => ({ ...prev, workspace_root: e.target.value }))} />
          </FormField>

          <FormField label="Scan Probe Workers" hint="Parallel metadata probes during discovery scans. Values above available CPU cores are clamped automatically.">
            <TextInput
              type="number"
              min={1}
              value={settings.scan_probe_workers}
              disabled={settings.discovery_method === 'watcher'}
              className={settings.discovery_method === 'watcher' ? 'cursor-not-allowed border-slate-700/70 bg-slate-900/40 text-slate-500' : ''}
              onChange={(e) => setSettings((prev) => ({ ...prev, scan_probe_workers: Number(e.target.value) }))}
            />
            {settings.discovery_method === 'watcher' && (
              <p className="text-xs text-slate-500">Watcher mode probes new files one at a time, so probe workers are not used.</p>
            )}
          </FormField>

          <FormField label="Minimum Free Disk (GB)" hint="Pause the queue when free disk drops below this threshold.">
            <TextInput type="number" min={1} value={settings.min_free_gb} onChange={(e) => setSettings((prev) => ({ ...prev, min_free_gb: Number(e.target.value) }))} />
          </FormField>

          <FormField label="Duplicate Cleanup Interval (Hours)" hint="How often scheduled duplicate optimized cleanup runs when enabled.">
            <TextInput
              type="number"
              min={1}
              value={settings.duplicate_cleanup_interval_hours}
              disabled={!settings.duplicate_cleanup_enabled}
              className={!settings.duplicate_cleanup_enabled ? 'cursor-not-allowed border-slate-700/70 bg-slate-900/40 text-slate-500' : ''}
              onChange={(e) => setSettings((prev) => ({ ...prev, duplicate_cleanup_interval_hours: Number(e.target.value) }))}
            />
          </FormField>
        </div>

        <div className="mt-4 space-y-3">
          <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Auto Discovery</p>
              <p className="text-xs text-slate-500">Automatically scan libraries for new media files.</p>
            </div>
            <Toggle
              checked={settings.auto_discovery_enabled}
              onChange={(e) => setSettings((prev) => ({ ...prev, auto_discovery_enabled: e.target.checked }))}
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Requeue Interrupted Jobs on Startup</p>
              <p className="text-xs text-slate-500">Automatically re-add jobs that were interrupted by an unexpected shutdown.</p>
            </div>
            <Toggle
              checked={settings.requeue_interrupted_jobs}
              onChange={(e) => setSettings((prev) => ({ ...prev, requeue_interrupted_jobs: e.target.checked }))}
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Clean Up Workspaces on Startup</p>
              <p className="text-xs text-slate-500">Remove leftover temporary encoding directories on startup.</p>
            </div>
            <Toggle
              checked={settings.cleanup_workspaces_on_startup}
              onChange={(e) => setSettings((prev) => ({ ...prev, cleanup_workspaces_on_startup: e.target.checked }))}
            />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Scheduled Duplicate Cleanup</p>
              <p className="text-xs text-slate-500">Automatically remove duplicate optimized/source artifacts on the configured interval.</p>
            </div>
            <Toggle
              checked={settings.duplicate_cleanup_enabled}
              onChange={(e) => setSettings((prev) => ({ ...prev, duplicate_cleanup_enabled: e.target.checked }))}
            />
          </div>
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <Btn variant="indigo" onClick={onRecoveryRun}>Run Recovery Now</Btn>
          <Btn variant="primary" onClick={onCleanupRun}>Run Workspace Cleanup</Btn>
          <Btn
            variant="warning"
            disabled={duplicateCleanupRunning}
            onClick={onDuplicateOptimizedCleanupRun}
          >
            {duplicateCleanupRunning
              ? 'Cleanup Running...'
              : 'Remove Duplicate Optimized Outputs'}
          </Btn>
          <Btn variant="warning" onClick={onOptimizedCleanupRun}>Remove Optimized Outputs</Btn>
        </div>

        <div className="mt-5">
          <Btn variant="primary" size="lg" disabled={savingSettings} onClick={saveSettings}>
            {savingSettings ? 'Saving…' : 'Save Settings'}
          </Btn>
        </div>
      </SettingsSectionCard>

      {/* Email Notifications */}
      <SettingsSectionCard title="Email Notifications" open={settingsSectionsOpen.notifications} onToggle={() => toggleSettingsSection('notifications')}>
        <div className="grid gap-4 md:grid-cols-2">
          <FormField label="SMTP Host">
            <TextInput type="text" value={notificationSettings.smtp_host} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_host: e.target.value }))} placeholder="smtp.example.com" />
          </FormField>
          <FormField label="SMTP Port">
            <TextInput type="number" min={1} max={65535} value={notificationSettings.smtp_port} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_port: Number(e.target.value) }))} />
          </FormField>
          <FormField label="SMTP Username">
            <TextInput type="text" value={notificationSettings.smtp_user} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_user: e.target.value }))} />
          </FormField>
          <FormField label="SMTP Password">
            <TextInput type="password" value={notificationSettings.smtp_password} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_password: e.target.value }))} />
          </FormField>
          <FormField label="From Email">
            <TextInput type="email" value={notificationSettings.from_email} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, from_email: e.target.value }))} placeholder="noreply@example.com" />
          </FormField>
          <div className="flex items-end pb-1">
            <Toggle
              checked={notificationSettings.smtp_tls}
              onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_tls: e.target.checked }))}
              label="Use TLS"
            />
          </div>
          <FormField label="Recipient Emails" hint="Comma or newline separated." span2>
            <textarea
              className="w-full rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm text-slate-100 outline-none transition-all duration-150 focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30"
              rows={3}
              value={notificationSettings.to_emails.join(', ')}
              onChange={(e) => setNotificationSettings((prev) => ({
                ...prev,
                to_emails: e.target.value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
              }))}
              placeholder="user@example.com, other@example.com"
            />
          </FormField>
        </div>

        <div className="mt-4">
          <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">Notify On</p>
          <div className="space-y-2">
            {[
              { key: 'job_complete', label: 'Job Complete' },
              { key: 'job_failed', label: 'Job Failed' },
              { key: 'job_interrupted', label: 'Job Interrupted' },
              { key: 'low_disk_pause', label: 'Low Disk Pause' },
              { key: 'recovery_ran', label: 'Recovery Ran' },
              { key: 'batch_complete', label: 'Batch Complete' },
            ].map(({ key, label }) => (
              <div key={key} className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-2.5">
                <span className="text-sm text-slate-200">{label}</span>
                <Toggle
                  checked={notificationSettings.notify_on[key]}
                  onChange={(e) => setNotificationSettings((prev) => ({ ...prev, notify_on: { ...prev.notify_on, [key]: e.target.checked } }))}
                />
              </div>
            ))}
          </div>
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <Btn variant="violet" disabled={savingSettings} onClick={saveNotificationSettings}>
            Save Notification Settings
          </Btn>
          <Btn variant="secondary" onClick={sendNotificationTest}>
            Send Test Email
          </Btn>
        </div>
      </SettingsSectionCard>

      {/* Prowlarr Integration */}
      {prowlarrSettings && (
        <SettingsSectionCard title="Prowlarr Integration" open={settingsSectionsOpen.prowlarr} onToggle={() => toggleSettingsSection('prowlarr')}>
          <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Enable Prowlarr</p>
              <p className="text-xs text-slate-500">Search Prowlarr indexers for pre-encoded releases instead of transcoding.</p>
            </div>
            <Toggle
              checked={prowlarrSettings.enabled}
              onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <FormField label="Prowlarr Host" hint="Protocol, hostname and port, e.g. http://192.168.1.100:9696" span2>
              <TextInput
                type="text"
                value={prowlarrSettings.host}
                onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, host: e.target.value }))}
                placeholder="http://localhost:9696"
              />
            </FormField>
            <FormField label="API Key" hint="Found in Prowlarr → Settings → General → API Key" span2>
              <TextInput
                type="password"
                value={prowlarrSettings.api_key}
                onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, api_key: e.target.value }))}
                placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              />
            </FormField>
          </div>
          {settings && (
            <div className="mt-5 border-t border-slate-800/70 pt-5">
              <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">Owned Torrent Cleanup</p>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="Check Interval (Seconds)" hint="How often Optimizarr checks owned qBittorrent torrents.">
                  <TextInput
                    type="number"
                    min={1}
                    value={settings.qbt_strike_check_interval_seconds}
                    onChange={(e) => setSettings((prev) => ({ ...prev, qbt_strike_check_interval_seconds: Number(e.target.value) }))}
                  />
                </FormField>
                <FormField label="Metadata Strikes" hint="Applies to public and private torrents stuck downloading metadata.">
                  <TextInput
                    type="number"
                    min={0}
                    value={settings.qbt_metadata_max_strikes}
                    onChange={(e) => setSettings((prev) => ({ ...prev, qbt_metadata_max_strikes: Number(e.target.value) }))}
                  />
                </FormField>
                <FormField label="Stalled Strikes" hint="Removes owned torrents in stalled or error states after this many strikes.">
                  <TextInput
                    type="number"
                    min={0}
                    value={settings.qbt_stalled_max_strikes}
                    onChange={(e) => setSettings((prev) => ({ ...prev, qbt_stalled_max_strikes: Number(e.target.value) }))}
                  />
                </FormField>
                <FormField label="Slow Speed (B/s)" hint="Owned torrents below this speed receive slow-download strikes. Set 0 to disable.">
                  <TextInput
                    type="number"
                    min={0}
                    value={settings.qbt_slow_min_speed_bps}
                    onChange={(e) => setSettings((prev) => ({ ...prev, qbt_slow_min_speed_bps: Number(e.target.value) }))}
                  />
                </FormField>
                <FormField label="Slow Strikes" hint="Removes owned slow torrents after this many strikes.">
                  <TextInput
                    type="number"
                    min={0}
                    value={settings.qbt_slow_max_strikes}
                    onChange={(e) => setSettings((prev) => ({ ...prev, qbt_slow_max_strikes: Number(e.target.value) }))}
                  />
                </FormField>
                <div className="flex items-end">
                  <div className="flex w-full items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                    <div>
                      <p className="text-sm font-medium text-slate-200">Ignore Private Slow Torrents</p>
                      <p className="text-xs text-slate-500">Private torrents still receive metadata and stalled strikes.</p>
                    </div>
                    <Toggle
                      checked={settings.qbt_slow_ignore_private}
                      onChange={(e) => setSettings((prev) => ({ ...prev, qbt_slow_ignore_private: e.target.checked }))}
                    />
                  </div>
                </div>
              </div>
              <div className="mt-4">
                <Btn variant="primary" disabled={savingSettings} onClick={saveSettings}>
                  {savingSettings ? 'Saving...' : 'Save Cleanup Rules'}
                </Btn>
              </div>
            </div>
          )}
          <div className="mt-5 flex flex-wrap gap-3">
            <Btn variant="violet" disabled={savingProwlarrSettings} onClick={saveProwlarrSettings}>
              {savingProwlarrSettings ? 'Saving…' : 'Save Prowlarr Settings'}
            </Btn>
            <Btn variant="secondary" disabled={testingProwlarrConnection} onClick={handleTestProwlarrConnection}>
              {testingProwlarrConnection ? 'Testing…' : 'Test Connection'}
            </Btn>
          </div>
        </SettingsSectionCard>
      )}

      {/* qBittorrent */}
      {qbtSettings && (
        <SettingsSectionCard title="qBittorrent" open={settingsSectionsOpen.qbittorrent} onToggle={() => toggleSettingsSection('qbittorrent')}>
          <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Enable qBittorrent</p>
              <p className="text-xs text-slate-500">Used for torrent releases from Prowlarr. Downloads tagged with "optimizarr" and left to follow qBittorrent's own seeding rules after import.</p>
            </div>
            <Toggle
              checked={qbtSettings.enabled}
              onChange={(e) => setQbtSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <FormField label="Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
              <TextInput
                type="text"
                value={qbtSettings.host}
                onChange={(e) => setQbtSettings((prev) => ({ ...prev, host: e.target.value }))}
                placeholder="http://localhost"
              />
            </FormField>
            <FormField label="Port">
              <TextInput
                type="number"
                min={1}
                max={65535}
                value={qbtSettings.port}
                onChange={(e) => setQbtSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
              />
            </FormField>
            <FormField label="Username">
              <TextInput
                type="text"
                value={qbtSettings.username}
                onChange={(e) => setQbtSettings((prev) => ({ ...prev, username: e.target.value }))}
                placeholder="admin"
              />
            </FormField>
            <FormField label="Password">
              <TextInput
                type="password"
                value={qbtSettings.password}
                onChange={(e) => setQbtSettings((prev) => ({ ...prev, password: e.target.value }))}
                placeholder="••••••••"
              />
            </FormField>
          </div>
          <div className="mt-5 flex flex-wrap gap-3">
            <Btn variant="violet" disabled={savingQbtSettings} onClick={saveQbtSettings}>
              {savingQbtSettings ? 'Saving…' : 'Save qBittorrent Settings'}
            </Btn>
            <Btn variant="secondary" disabled={testingQbtConnection} onClick={handleTestQbtConnection}>
              {testingQbtConnection ? 'Testing…' : 'Test Connection'}
            </Btn>
          </div>
        </SettingsSectionCard>
      )}

      {/* SABnzbd */}
      {sabSettings && (
        <SettingsSectionCard title="SABnzbd" open={settingsSectionsOpen.sabnzbd} onToggle={() => toggleSettingsSection('sabnzbd')}>
          <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
            <div>
              <p className="text-sm font-medium text-slate-200">Enable SABnzbd</p>
              <p className="text-xs text-slate-500">Used for usenet/NZB releases from Prowlarr. History entry is automatically removed from SABnzbd after a successful import.</p>
            </div>
            <Toggle
              checked={sabSettings.enabled}
              onChange={(e) => setSabSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <FormField label="Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
              <TextInput
                type="text"
                value={sabSettings.host}
                onChange={(e) => setSabSettings((prev) => ({ ...prev, host: e.target.value }))}
                placeholder="http://localhost"
              />
            </FormField>
            <FormField label="Port">
              <TextInput
                type="number"
                min={1}
                max={65535}
                value={sabSettings.port}
                onChange={(e) => setSabSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
              />
            </FormField>
            <FormField label="API Key" span2>
              <TextInput
                type="password"
                value={sabSettings.api_key}
                onChange={(e) => setSabSettings((prev) => ({ ...prev, api_key: e.target.value }))}
                placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              />
            </FormField>
          </div>
          <div className="mt-5 flex flex-wrap gap-3">
            <Btn variant="violet" disabled={savingSabSettings} onClick={saveSabSettings}>
              {savingSabSettings ? 'Saving…' : 'Save SABnzbd Settings'}
            </Btn>
            <Btn variant="secondary" disabled={testingSabConnection} onClick={handleTestSabConnection}>
              {testingSabConnection ? 'Testing…' : 'Test Connection'}
            </Btn>
          </div>
        </SettingsSectionCard>
      )}

      {/* Plex Integration */}
      <SettingsSectionCard title="Plex Integration" open={settingsSectionsOpen.plex} onToggle={() => toggleSettingsSection('plex')}>
        <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
          <div>
            <p className="text-sm font-medium text-slate-200">Enable Plex Scan</p>
            <p className="text-xs text-slate-500">Trigger a Plex library scan after each file finishes encoding.</p>
          </div>
          <Toggle
            checked={plexSettings.enabled}
            onChange={(e) => setPlexSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
          />
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <FormField label="Plex Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
            <TextInput
              type="text"
              value={plexSettings.host}
              onChange={(e) => setPlexSettings((prev) => ({ ...prev, host: e.target.value }))}
              placeholder="http://localhost"
            />
          </FormField>
          <FormField label="Port">
            <TextInput
              type="number"
              min={1}
              max={65535}
              value={plexSettings.port}
              onChange={(e) => setPlexSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
            />
          </FormField>
          <FormField label="Plex Token" hint="Found in Plex account settings under Authorized Devices." span2>
            <TextInput
              type="password"
              value={plexSettings.token}
              onChange={(e) => setPlexSettings((prev) => ({ ...prev, token: e.target.value }))}
              placeholder="xxxxxxxxxxxxxxxxxxxx"
            />
          </FormField>
        </div>
        {plexLibraries.length > 0 && (
          <div className="mt-4">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">Discovered Sections</p>
            <div className="space-y-1">
              {plexLibraries.map((section) => (
                <div key={section.id} className="flex items-center gap-2 rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-2 text-sm">
                  <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs font-mono text-slate-300">{section.id}</span>
                  <span className="text-slate-200">{section.name}</span>
                  <span className="ml-auto text-xs text-slate-500">{section.type}</span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-xs text-slate-500">Assign sections to each library in the Libraries tab.</p>
          </div>
        )}
        <div className="mt-5 flex flex-wrap gap-3">
          <Btn variant="violet" disabled={savingPlexSettings} onClick={savePlexSettings}>
            {savingPlexSettings ? 'Saving…' : 'Save Plex Settings'}
          </Btn>
          <Btn variant="secondary" disabled={testingPlexConnection} onClick={handleTestPlexConnection}>
            {testingPlexConnection ? 'Testing…' : 'Test Connection'}
          </Btn>
          <Btn variant="secondary" disabled={loadingPlexLibraries} onClick={loadPlexLibraries}>
            {loadingPlexLibraries ? 'Loading…' : 'Load Sections'}
          </Btn>
        </div>
      </SettingsSectionCard>
    </section>
  );
}

export default memo(SettingsPage);
