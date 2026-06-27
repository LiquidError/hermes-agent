import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { SETTINGS_ROUTE } from '@/app/routes'
import { Button } from '@/components/ui/button'
import { ErrorIcon } from '@/components/ui/error-state'
import { LogView } from '@/components/ui/log-view'
import type { DesktopConnectionConfig } from '@/global'
import { useI18n } from '@/i18n'
import { FileText, Loader2, LogIn, RefreshCw, Settings } from '@/lib/icons'
import { $desktopBoot } from '@/store/boot'
import { notify, notifyError } from '@/store/notifications'
import { $desktopOnboarding } from '@/store/onboarding'

import type { RemoteReauth } from './boot-failure-reauth'
import { deriveProviderShape, isRemoteReauthFailure, signInLabel } from './boot-failure-reauth'

type BusyAction = 'retry' | 'signin' | null

// This is the remote-only client: it never runs a local backend, so the boot
// failure surface routes to the remote-gateway settings instead of offering
// install-recovery (Repair/Use-local-gateway), which don't exist here.
//
// Three shapes:
//   - needsRemoteSetup: no remote configured yet (first run). Route the user to
//     Settings → Gateway → Remote gateway — the only thing that makes the app
//     work. Without this they'd be told to open Settings but couldn't reach it.
//   - remoteReauth: a configured remote whose session lapsed. Offer "Sign in".
//   - generic: a configured remote that failed to connect (backend down,
//     unreachable URL). Offer Retry + a shortcut back to the gateway settings.
export function BootFailureOverlay() {
  const boot = useStore($desktopBoot)
  const onboarding = useStore($desktopOnboarding)
  const navigate = useNavigate()
  const location = useLocation()
  const { t } = useI18n()
  const [busy, setBusy] = useState<BusyAction>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [showLogs, setShowLogs] = useState(false)
  const [remoteReauth, setRemoteReauth] = useState<RemoteReauth | null>(null)
  const [needsRemoteSetup, setNeedsRemoteSetup] = useState(false)

  const visible = Boolean(boot.error) && !boot.running
  // While first-run onboarding owns the flow we let it surface its own progress.
  const suppressed = onboarding.flow.status !== 'idle' && onboarding.flow.status !== 'error'
  // When the user follows our "Set up remote gateway" button into the settings
  // overlay, hide this one so it doesn't sit on top of the page we sent them to.
  // boot.error stays set until they connect, so closing settings without
  // configuring brings the overlay back — correct.
  const settingsOpen = location.pathname.startsWith(SETTINGS_ROUTE)

  useEffect(() => {
    if (!visible) {
      return
    }

    void window.hermesDesktop
      ?.getRecentLogs()
      .then(res => setLogs(res.lines ?? []))
      .catch(() => undefined)
  }, [visible])

  // Classify the failure from the connection config: reauth vs. not-configured
  // vs. generic. Runs whenever the overlay becomes visible.
  useEffect(() => {
    if (!visible) {
      setRemoteReauth(null)
      setNeedsRemoteSetup(false)

      return
    }

    let cancelled = false

    void (async () => {
      const desktop = window.hermesDesktop

      if (!desktop?.getConnectionConfig) {
        // No way to read config — assume first-run setup is needed.
        if (!cancelled) {
          setNeedsRemoteSetup(true)
        }

        return
      }

      let config: DesktopConnectionConfig

      try {
        config = await desktop.getConnectionConfig()
      } catch {
        if (!cancelled) {
          setNeedsRemoteSetup(true)
        }

        return
      }

      if (cancelled) {
        return
      }

      // A configured remote with a lapsed OAuth session → offer "Sign in".
      if (isRemoteReauthFailure(config)) {
        setNeedsRemoteSetup(false)

        // Best-effort probe so the button copy matches the login window
        // (password form vs OAuth redirect). Probe failure keeps generic copy.
        let shape = deriveProviderShape(null)

        try {
          const probe = await desktop.probeConnectionConfig(config.remoteUrl)
          shape = deriveProviderShape(probe?.providers)
        } catch {
          // Generic copy is fine.
        }

        if (!cancelled) {
          setRemoteReauth({ url: config.remoteUrl, ...shape })
        }

        return
      }

      // No usable remote configured yet → first-run setup.
      const hasRemote = config.mode === 'remote' && Boolean(config.remoteUrl)
      setRemoteReauth(null)
      setNeedsRemoteSetup(!hasRemote)
    })()

    return () => {
      cancelled = true
    }
  }, [visible])

  if (!visible || suppressed || settingsOpen) {
    return null
  }

  const retry = async () => {
    setBusy('retry')
    await window.hermesDesktop?.resetBootstrap().catch(() => undefined)
    window.location.reload()
  }

  // Route to Settings → Gateway → Remote gateway. The settings overlay opens on
  // top; this overlay hides itself while it's open (see settingsOpen above).
  const openRemoteGatewaySettings = () => navigate(`${SETTINGS_ROUTE}?tab=gateway`)

  // Open the gateway's login window (username/password form or OAuth redirect).
  // On success the session cookie is re-established; reload so boot re-runs.
  const signInRemote = async () => {
    if (!remoteReauth) {
      return
    }

    setBusy('signin')

    try {
      const result = await window.hermesDesktop?.oauthLoginConnectionConfig(remoteReauth.url)

      if (result?.connected) {
        notify({ kind: 'success', title: t.boot.failure.signedInTitle, message: t.boot.failure.signedInMessage })
        window.location.reload()

        return
      }

      notify({
        kind: 'warning',
        title: t.boot.failure.signInIncompleteTitle,
        message: t.boot.failure.signInIncompleteMessage
      })
    } catch (err) {
      notifyError(err, t.boot.failure.signInFailed)
    } finally {
      setBusy(null)
    }
  }

  const openLogs = () => void window.hermesDesktop?.revealLogs().catch(() => undefined)
  const copy = t.boot.failure

  const signInButtonLabel = signInLabel(remoteReauth, {
    identityProvider: copy.identityProvider,
    remoteGateway: copy.signInToRemoteGateway,
    withProvider: copy.signInWithProvider
  })

  const title = needsRemoteSetup ? copy.setupTitle : remoteReauth ? copy.remoteTitle : copy.title
  const description = needsRemoteSetup ? copy.setupDescription : remoteReauth ? copy.remoteDescription : copy.description
  const hint = needsRemoteSetup ? copy.setupHint : remoteReauth ? copy.remoteSignInHint : copy.genericHint

  return (
    <div className="fixed inset-0 z-[1400] flex items-center justify-center bg-(--ui-chat-surface-background) p-6">
      <div className="w-full max-w-[40rem] overflow-hidden rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) shadow-nous">
        <div className="flex items-start gap-3 px-5 py-4">
          <ErrorIcon className="mt-0.5" size="1.25rem" />
          <div>
            <h2 className="text-[0.9375rem] font-semibold tracking-tight">{title}</h2>
            <p className="mt-1 text-[0.8125rem] leading-5 text-(--ui-text-tertiary)">{description}</p>
          </div>
        </div>

        <div className="grid gap-4 p-5">
          <div className="rounded-2xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-xs text-destructive">
            {boot.error}
          </div>

          <div className="grid gap-2">
            <div className="flex flex-wrap gap-2">
              {needsRemoteSetup ? (
                <Button disabled={Boolean(busy)} onClick={openRemoteGatewaySettings}>
                  <Settings className="size-4" />
                  {copy.setUpRemoteGateway}
                </Button>
              ) : remoteReauth ? (
                <>
                  <Button disabled={Boolean(busy)} onClick={() => void signInRemote()}>
                    {busy === 'signin' ? <Loader2 className="size-4 animate-spin" /> : <LogIn className="size-4" />}
                    {signInButtonLabel}
                  </Button>
                  <Button disabled={Boolean(busy)} onClick={openRemoteGatewaySettings} variant="outline">
                    <Settings className="size-4" />
                    {copy.remoteGatewaySettings}
                  </Button>
                </>
              ) : (
                <>
                  <Button disabled={Boolean(busy)} onClick={() => void retry()}>
                    {busy === 'retry' ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                    {copy.retry}
                  </Button>
                  <Button disabled={Boolean(busy)} onClick={openRemoteGatewaySettings} variant="outline">
                    <Settings className="size-4" />
                    {copy.remoteGatewaySettings}
                  </Button>
                </>
              )}
              <Button onClick={openLogs} variant="ghost">
                <FileText />
                {copy.openLogs}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{hint}</p>
          </div>

          {logs.length > 0 ? (
            <div className="grid gap-2">
              <Button
                className="-ml-2 self-start font-medium"
                onClick={() => setShowLogs(v => !v)}
                size="xs"
                type="button"
                variant="text"
              >
                {showLogs ? copy.hideRecentLogs : copy.showRecentLogs}
              </Button>
              {showLogs ? <LogView className="max-h-48">{logs.slice(-40).join('')}</LogView> : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
