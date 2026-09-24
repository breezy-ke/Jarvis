# Jarvis runbook

How to run, check, update, back up and fix Jarvis. Run the commands in Ubuntu
(WSL), in `~/Jarvis`, unless a step says PowerShell.

## Everyday commands

| Command | What it does |
|---|---|
| `make doctor` | Checks everything (host, Windows, Tailscale, keys, database, models) and prints the fix for each problem |
| `make doctor ONLINE=1` | Also tests your API keys and that configured model IDs still exist |
| `make ps` | Shows what's running |
| `make logs` | Follows the core logs (`make logs SERVICE=ollama` for another service) |
| `make restart` | Restarts the core; needed after editing `.env` or `config/` |
| `make up` / `make down` | Starts / stops Jarvis (your data stays in Docker volumes) |
| `make setup-token` | Prints a new first-run setup code |
| `make pull-models` | Downloads the local models named in `config/models.yaml` |
| `make update` | Pulls the latest code, rebuilds, restarts |
| `make backup` / `make restore FILE=…` | Database backup and restore (see below) |

The trace viewer (every AI call, with prompts and replies) is at
`http://localhost:6006` on the PC only. It never leaves your machine.

## Kill switch

To stop Jarvis acting on its own right away, do either of these:

- Home > **Stand down**
- Settings > Kill switch

While it's on, **nothing executes**, not even actions you approve: they wait
until you release it. Chat still works. **Release** lets approved actions run
and turns autonomy back on.

The switch is stored in the database, so it survives restarts. From a
terminal, `make down` stops Jarvis completely.

## Updating

```bash
make update     # git pull, rebuild, restart; the database migrates itself
make doctor
```

If an update changes `.env.example`:

1. Compare it with your `.env` and copy over any new settings.
2. Run `make secrets`. It fills in newly added generated secrets and never
   touches the values you already have.

## Backups

`make backup` writes `data/backups/jarvis-<date>.dump`: the whole database,
including memory, profile, approvals and the audit log.

- **It contains personal data.** Keep it private.
- **Keep a copy of `JARVIS_SECRET_KEY`** from `.env` somewhere safe, apart from
  the backup. It encrypts your OAuth tokens: without it, a restored database
  works, but you'll need to connect Google again.
- **Restore:** `make restore FILE=data/backups/jarvis-….dump`. It asks you to
  type `restore`, stops the core, replaces the database, and starts the core
  again.
- **Restore drill:** do one every few months, to prove the backups work.
  1. `make backup`
  2. `make restore FILE=<that file>`
  3. Check that the "What I know" page and the Activity log look right. Use
     **Verify integrity** on the Activity page.

Nightly off-site encrypted backups (Google Drive or Backblaze B2) arrive in
Phase 8.

## Reboot test

Proves Jarvis comes back after a power cut with nobody logged in:

1. Restart Windows and **don't log in**.
2. After 2 minutes, open Jarvis on your phone over mobile data. It should load.
3. Log in and run `make doctor`: everything should be ✔.

## Jarvis doesn't come back after a reboot

Work through these in order:

1. **Check that the boot task ran.** In PowerShell:

   ```powershell
   Get-ScheduledTask Jarvis | Get-ScheduledTaskInfo
   ```

   - `LastTaskResult` 267009 means it's running, which is good.
   - Other codes: open Task Scheduler > Task Scheduler Library > Jarvis >
     History.
2. **Check that Ubuntu started it.** In Ubuntu, `cat /var/log/jarvis-boot.log`
   gets a line each time the boot task starts WSL.
3. **Check the service and containers:**

   ```bash
   systemctl status jarvis docker
   make ps
   ```

4. **Nothing ran before you logged in?** The stored-password logon may be
   blocked on this PC, or the password changed. Run
   `scripts/windows/setup.ps1` again, which re-registers the task. If you used
   `-NoStoredPassword`, try without it.
5. **Fast Startup:** after a normal "Shut down", Windows skips startup tasks if
   Fast Startup is on. The setup script turns it off, but Windows updates can
   turn it back on. `make doctor` checks for this.

## Lost access

- **Lost one device:**
  1. Sign in on another device.
  2. Settings > Passkeys and recovery: remove the lost device's passkey.
  3. Make **New recovery codes** if the lost device could see them.
  4. The lost device may still be signed in, so sign out every device:

     ```bash
     docker compose exec -T postgres psql -U jarvis -d jarvis -c "DELETE FROM auth_sessions;"
     ```

     Then sign in again on the devices you still have.
- **Lost every passkey:** Sign in > **Use a recovery code**.
- **Lost every passkey and every recovery code:** only someone with access to
  the PC can get back in.

  ```bash
  docker compose exec -T postgres psql -U jarvis -d jarvis \
    -c "DELETE FROM webauthn_credentials;" -c "DELETE FROM auth_sessions;"
  make setup-token
  ```

  Then register a new passkey with that code, just like the first time.
  - This works because a setup code is only accepted while no passkey exists.
    That's also why deleting the passkeys is needed first.
  - It's safe because the database is reachable only from the PC itself.

## Google sign-in stopped working

1. Check the OAuth consent screen is **In production**. In "Testing" status,
   tokens expire every 7 days.
2. In Jarvis > Sources: **Disconnect**, then **Connect Google** again, from a
   browser on the PC.
3. If Google says the client was deleted or the secret changed, update
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`, then
   `make restart`.

## Changing AI models

Everything is in `config/models.yaml`: providers, models, and which models
each task may use, in order.

- **A model was retired:** `make doctor ONLINE=1` lists the ones each provider
  offers now. Change `model:` and run `make restart`.
- **Bigger GPU:** `make doctor` recommends a local model size for your VRAM.
  1. Change `local-chat` → `model:`.
  2. `make pull-models`
  3. `make restart`
- **Add a paid model:**
  1. Set `budget.monthly_usd_cap` above 0.
  2. Add the key to `.env`.
  3. Add the model to the tasks that should use it.

  The router refuses to send personal or confidential data to any provider
  whose flags don't allow it (`trains_on_data`, `zero_data_retention`), and
  Jarvis won't start if a task breaks those rules.

## Changing what Jarvis may do

`config/policies.yaml` sets, for each kind of action:

- its autonomy level
- its risk level
- its validators
- its undo window

Run `make restart` after editing. Some rules can't be relaxed, and Jarvis
refuses to start if the file tries:

- money, deletions and production deploys can never run automatically
- high-risk approvals need a passkey tap

## Rotating secrets

| Secret | How |
|---|---|
| API keys (Groq, Gemini, …) | Make a new key at the provider, put it in `.env`, `make restart`, then delete the old key |
| `POSTGRES_PASSWORD` | Change it inside Postgres first (`ALTER USER jarvis PASSWORD '…'`), then in `.env`, then `make up` |
| `JARVIS_SECRET_KEY` | Don't, unless it leaked: a new key can't read existing OAuth tokens, so reconnect Google afterwards |
| VAPID keys | Changing them unsubscribes every device from notifications; re-enable them in Settings afterwards |

## Checks per phase

Run these on your PC after each phase is merged. CI covers the automated parts
with synthetic data. These checks use your real setup.

### Phase 0 and 1 (foundations, "know me first")

1. `make doctor`: everything ✔, apart from warnings for things you chose not to
   set up.
2. Sign in on the PC and on your phone. Add Jarvis to the phone's home screen.
   Notifications: **Send a test** arrives on the phone.
3. In chat, ask a question. The reply streams in. Activity shows the model
   call; the trace viewer (`http://localhost:6006`) shows the prompt.
4. **Private data stays private.**
   1. Remove `GROQ_API_KEY` from `.env` and `make restart`.
   2. Stop Ollama (`docker compose stop ollama`) and send a chat message.
   3. Jarvis must say no model is available. It must never fall back to
      Gemini, which is public-only.
   4. Restore the key, `make up`, `make restart`.
5. **Approvals:**
   1. Engage the kill switch.
   2. Ask Jarvis to send you a notification.
   3. It must wait in Approvals.
   4. Approve it: it still must not arrive while the kill switch is on.
   5. Release the kill switch: the notification arrives within seconds.
6. **Memory:**
   1. Tell Jarvis a preference ("I prefer Tailwind over Bootstrap").
   2. Within 5 minutes it appears under "What I know" > **To review**.
   3. Accept it, then ask "What CSS framework do I prefer?".
   4. Delete the fact and ask again: Jarvis no longer knows it.
7. **Onboarding:**
   1. Complete the modules.
   2. Review the summary on "What I know" and sign it off.
   3. Home should show the profile as signed off.
8. **Sources:**
   1. Run the Gmail style source.
   2. Check the suggestions it creates are statistics about your writing, not
      copies of emails.
9. Activity > **Verify integrity**: "Log intact".
10. The reboot test (above).

Later phases add their own checks here as they ship. For example, the email
phase will include sending yourself a prompt-injection email and confirming
Jarvis proposes no action from it.
