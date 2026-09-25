# Setting up Jarvis on your PC

This guide takes you from a Windows PC to a working Jarvis you can reach from
your phone and talk to. Plan on about an hour and a half for steps 1 to 7, plus
60 to 90 minutes for the onboarding interview in step 8, which you can split
across sessions.

Everything runs on your PC. Your keys and passwords go into files on that PC
only. Never paste them into a chat, an issue or a commit.

| Step | What you get |
|---|---|
| 1. Prepare Windows | WSL2 + Ubuntu, Docker with your GPU, Tailscale, Jarvis running, boot task |
| 2. First sign-in | Your passkey and recovery codes; Jarvis on your phone's home screen |
| 3. Google (Gmail + Calendar) | Read-only Gmail and Calendar access, for learning your style and schedule |
| 4. AI keys | Groq (private-safe cloud models) and Gemini (public data only) |
| 5. Optional sources | GitHub, your website, your CV or LinkedIn export |
| 6. Voice | Talk to Jarvis in the app; "Hey Jarvis" on the PC |
| 7. Telegram (optional) | Chat and approve everyday actions from Telegram |
| 8. Onboarding interview | Jarvis learns who you are; autonomy stays off until you sign it off |
| 9. Reboot test | Proof that Jarvis comes back by itself after a power cut |

`make doctor` checks each step and tells you exactly what to fix.

---

## Before you start

- **Windows 11**, or Windows 10 21H2 or later, on the PC that stays on.
- **The latest NVIDIA driver**, installed on Windows from nvidia.com. It includes
  WSL support. Never install an NVIDIA driver inside Ubuntu.
- **Virtualisation switched on** in the BIOS/UEFI. It's called Intel VT-x,
  Intel Virtualization Technology or AMD SVM, and it's usually on already. Task
  Manager > Performance > CPU shows "Virtualization: Enabled".
- **An administrator Windows account**: the one you normally use.
- **The Jarvis repository on the PC.** Either:
  - install [Git for Windows](https://git-scm.com/download/win) and clone it:
    `git clone https://github.com/breezy-ke/Jarvis.git`, or
  - download the ZIP from GitHub and extract it, for example to `C:\Users\<you>\Jarvis`.
- **Docker Desktop, only if you already have it:** uninstall it, or at least
  turn off its WSL integration. Jarvis uses Docker Engine inside Ubuntu, which
  is more reliable for GPUs and doesn't need you to be logged in.

---

## Step 1: Prepare Windows (one script)

1. Open **Windows PowerShell as administrator**: Start menu > type
   "PowerShell" > right-click > Run as administrator.
2. Go to the Jarvis folder and run the setup script:

   ```powershell
   cd C:\Users\<you>\Jarvis
   powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
   ```

3. Follow what it prints. It is safe to run again at any point; every step
   checks what's already done. Along the way:
   - **First run:** it turns on WSL and asks you to **restart Windows**. Run it
     again after the restart.
   - **Ubuntu** installs and asks you to **create a Linux username and
     password**. If you then see an Ubuntu prompt, type `exit` to continue.
   - **Docker and the GPU:** it installs Docker Engine and the NVIDIA Container
     Toolkit inside Ubuntu, then runs a GPU test (`nvidia-smi` inside a
     container).
   - **Tailscale** installs and opens a browser for you to **sign in**. Use the
     same account you'll use on your phone. If it asks you to enable HTTPS
     certificates or Serve for your tailnet, open the link and approve.
   - **The first start** builds Jarvis and downloads the speech server, which
     takes several minutes. Then the local AI model (about 5 GB) and the
     speech models (about 2 GB) download.
   - **The boot task** asks for your **Windows password**. That's the
     Microsoft account password if you sign in with one, not your PIN. Windows
     stores it so the task can start Jarvis before anyone logs in. Jarvis never
     sees it. If your organisation forbids stored passwords, run the script
     with `-NoStoredPassword`, then check with the reboot test (step 9).
4. At the end it runs `make doctor` and prints your **setup code**. Keep that
   window open for step 2.

What the script changed on Windows:

- `%UserProfile%\.wslconfig`: WSL keeps running when idle (`vmIdleTimeout=-1`,
  `instanceIdleTimeout=-1`) and gets 60% of your RAM. Your old file is backed
  up next to it.
- **The "Jarvis" task in Task Scheduler.** It runs at startup, and at log-on
  as a fallback.
- **Power:** no sleep and no hibernate on mains power, and Fast Startup off.
  Fast Startup makes "Shut down" a kind of hibernation, after which startup
  tasks don't run.
- **Tailscale** is installed, set to "Run unattended", and serves
  `https://<your-pc>.<tailnet>.ts.net` to Jarvis (`tailscale serve --bg 8080`).

Two more things only you can do:

- **In the Tailscale admin console** (login.tailscale.com > Machines > your PC
  > ⋯): choose **Disable key expiry**. Otherwise the PC drops off your tailnet
  after 180 days.
- **Protect the disk and the power:**
  - Turn on BitLocker or Device Encryption (Settings > Privacy & security >
    Device encryption).
  - In the BIOS, set **Restore on AC power loss** (sometimes "AC Back") to
    **Power On**.
  - A small UPS is worth it if power cuts are common.

<details>
<summary>Doing it by hand instead (or on another Linux machine)</summary>

In Ubuntu (WSL):

```bash
git clone https://github.com/breezy-ke/Jarvis.git ~/Jarvis
cd ~/Jarvis
sudo scripts/wsl/bootstrap.sh     # Docker Engine, NVIDIA toolkit, systemd, autostart
# open a new Ubuntu window so your account picks up the docker group
make secrets                      # creates .env with generated secrets
nano .env                         # set JARVIS_PUBLIC_ORIGIN (see below)
make up && make pull-models && make doctor
```

`JARVIS_PUBLIC_ORIGIN` must be the exact HTTPS address you open Jarvis at,
because passkeys are bound to it. With Tailscale, run `tailscale serve --bg 8080`
on Windows, then copy the `https://…ts.net` address that `tailscale serve status`
prints.
</details>

---

## Step 2: First sign-in and your phone

1. On the PC, open `https://<your-pc>.<tailnet>.ts.net`. The setup script
   printed this address, and it's also in `~/Jarvis/.env` as
   `JARVIS_PUBLIC_ORIGIN`.
2. Enter the **setup code**. It's valid for 24 hours. For a new one, run
   `make setup-token` in Ubuntu, or read it from `make logs`.
3. Choose **Create passkey** and confirm with Windows Hello (face, fingerprint
   or PIN).
4. **Save the 10 recovery codes** somewhere safe that isn't this PC, such as a
   password manager or a printout. Each code works once, and they're how you
   get back in if you lose every passkey.
5. **On your phone:**
   1. Install the Tailscale app and sign in with the same account.
   2. Open the same address. A Windows Hello passkey stays on the PC, so tap
      **Use a recovery code** and enter one of your codes.
   3. Go to **Settings > Passkeys and recovery > Add a passkey on this device**.
      You'll have 9 codes left; **New recovery codes** on the same card makes a
      fresh set.
   4. Add Jarvis to your home screen: in Safari, Share > Add to Home Screen; in
      Chrome, ⋮ > Install app.
   5. Open it from the home screen, then go to **Settings > Notifications**,
      tap **Enable on this device**, then **Send a test**.

   Tip: if your phone keeps passkeys in iCloud Keychain or Google Password
   Manager, you can do this step the other way round. Create the first passkey
   on the phone, typing in the setup code. Then on the PC choose **Sign in**
   and pick "use a phone or tablet" to sign in with a QR code. No recovery code
   is used.

---

## Step 3: Google (Gmail and Calendar)

For now Jarvis only **reads**: your sent mail (to learn your writing style) and
your calendar (to learn your routines). Sending email arrives in Phase 3, and
even then only with your approval.

1. Go to [console.cloud.google.com](https://console.cloud.google.com), signed
   in with the Gmail account Jarvis should use, and **create a project** named
   "Jarvis".
2. **APIs & Services > Library:** enable the **Gmail API** and the **Google
   Calendar API**.
3. **Google Auth Platform (OAuth consent screen):**
   1. App name "Jarvis", with your own email as the support and developer
      contact. Audience: **External**.
   2. **Data access / Scopes:** add `…/auth/gmail.readonly` and
      `…/auth/calendar.readonly`.
   3. **Audience:** click **Publish app** so the status is **In production**.
      This matters: in "Testing" status Google expires your sign-in every
      7 days. You don't need Google's verification for an app only you use.
4. **Clients > Create client**, with application type **Desktop app** and the
   name "Jarvis on my PC". Copy the **Client ID** and **Client secret**.
5. In Ubuntu, add them to `~/Jarvis/.env` and restart Jarvis:

   ```bash
   cd ~/Jarvis && nano .env
   # GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
   # GOOGLE_OAUTH_CLIENT_SECRET=...
   make restart
   ```

6. **On the PC itself** (not your phone), open Jarvis > **Sources** >
   **Connect Google**.
   - Google shows "Google hasn't verified this app". That's expected for your
     own app: click **Advanced > Go to Jarvis (unsafe)** and allow read-only
     access.
   - Google then sends you back to `http://127.0.0.1:8080/...`, which only
     works on the PC. That's why this step happens there.

Jarvis stores the Google tokens encrypted with `JARVIS_SECRET_KEY`. You can
disconnect in Sources at any time, and also remove access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

---

## Step 4: AI keys

Jarvis works with just the local model on your GPU. These free keys add faster
and bigger models. Each provider is only sent the data its terms allow, and
the router refuses anything else. `docs/security.md` has the full table.

1. **Groq** (private-safe: no training on your data, even on the free tier):
   1. Create a key at [console.groq.com/keys](https://console.groq.com/keys).
   2. In **Settings > Data Controls**, turn on **Zero Data Retention**.
      `config/models.yaml` assumes it's on, and `make doctor` reminds you.
2. **Gemini** (Google AI Studio free tier): create a key at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Google
   may use free-tier prompts to improve its products, and people may review
   them. So Jarvis only ever sends Gemini **public** data, such as news for your
   briefs.
3. **OpenRouter** (optional, public data only): create a key at
   [openrouter.ai/keys](https://openrouter.ai/keys).
4. Add the keys to `~/Jarvis/.env`:

   ```bash
   GROQ_API_KEY=...
   GEMINI_API_KEY=...
   OPENROUTER_API_KEY=...   # optional
   ```

5. Run `make restart`, then `make doctor ONLINE=1`. The online check tests each
   key and confirms the configured models still exist. Model names change
   often, and when they do the doctor suggests replacements for
   `config/models.yaml`.

Paid models (Claude, GPT) stay switched off until you set a monthly budget in
`config/models.yaml` (`budget.monthly_usd_cap`) and add the key.

---

## Step 5: Optional sources

In Jarvis > **Sources**, switch on only what you want Jarvis to learn from.
Each source reads only what its card describes, and everything it finds
arrives as a **suggestion** on the "What I know" page, for you to accept or
reject.

| Source | Reads | Needs |
|---|---|---|
| Writing style from sent mail | Your last ~200 sent emails; keeps only style statistics | Step 3 |
| Working patterns | 60 days of calendar event times | Step 3 |
| GitHub | Repository languages and manifest files | Your GitHub username (plus `GITHUB_TOKEN` in `.env` for private repos: a fine-grained, read-only token) |
| Your website | Up to 6 public pages, respecting robots.txt | Its address |
| CV or LinkedIn export | A PDF/TXT CV, or LinkedIn's "Download your data" ZIP | The file |

---

## Step 6: Voice

Speech runs on your PC. A speech server hears you (faster-whisper) and speaks
as Jarvis (Kokoro), in its own container next to Jarvis, so what you say never
leaves the PC. Step 1 already started it and downloaded its models.

**In the app:**

1. Open **Settings > Voice**. "Speech server" should say **ready**. Tap
   **Play a sample** to hear Jarvis.
   - To choose another voice, change `voice:` in `config/voice.yaml`, then
     `make restart`. The British male voices are `bm_george`, `bm_lewis`,
     `bm_daniel` and `bm_fable`.
2. Open **Talk**, tap the orb and allow the microphone. Ask something.
   - To interrupt Jarvis, just talk over it, or tap the orb.
   - **Hands-free** is the default. **Hold to talk** helps in a noisy room.
   - On speakers without headphones, if Jarvis keeps interrupting itself, turn
     off **Talk over Jarvis to interrupt** and tap the orb instead.
   - What you said and heard is saved as a normal conversation: **Open in
     Chat** shows it.
3. When Jarvis reads an action back to you, say **"confirm"** or **"cancel"**.
   - Only the exact phrases in `config/voice.yaml` count. Anything else is
     taken as a new request, and the action keeps waiting in Approvals.
   - High-risk actions can never be approved by voice. They need the app and
     your passkey.
4. "Jarvis, stand down" engages the kill switch.

**"Hey Jarvis" on the PC (optional).** A small tray app listens for the wake
word on the PC itself. Until it hears it, nothing leaves the PC.

1. In Jarvis, go to **Settings > Voice > Pair the Windows app**. After a
   passkey tap it shows a code, which works once, within 10 minutes.
2. In a normal **Windows PowerShell** window (not as administrator), from the
   Jarvis folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-satellite.ps1
   ```

   Type the code when it asks. The app starts now and at every logon; look for
   the round icon in the system tray.
3. Windows must let desktop apps use the microphone: Settings > Privacy &
   security > Microphone > **Let desktop apps access your microphone**.
4. Say "Hey Jarvis", wait for the chime, and ask something. Mute it from the
   tray menu or with **Ctrl+Alt+J**.

[satellite/README.md](../satellite/README.md) covers the settings, the
wake-word check and troubleshooting.

**No NVIDIA GPU?** Speech then runs on the processor, and answers take longer.
Switch to a smaller hearing model: set `stt_model:
Systran/faster-whisper-small` in `config/voice.yaml`, then run
`make pull-models && make restart`.

---

## Step 7: Telegram (optional)

Chat with Jarvis from Telegram by text or voice note, and approve everyday
actions with a tap. Jarvis fetches its Telegram messages itself, so it still
needs no public address.

1. In Telegram, message **@BotFather**, send `/newbot`, and choose a name and
   a username for your bot. Copy the token it gives you. It looks like
   `123456789:AA…`, and anyone who has it controls your bot, so keep it
   secret.
2. Add it to `~/Jarvis/.env` and restart Jarvis:

   ```bash
   cd ~/Jarvis && nano .env
   # TELEGRAM_BOT_TOKEN=123456789:AA...
   make restart
   ```

3. In Jarvis, go to **Settings > Telegram > Link Telegram** and confirm with
   your passkey. Then tap **Open Telegram**, and **Start** in the chat that
   opens. (Or send the `/start` message the card shows to your bot.) The link
   works once, within 10 minutes.
4. Tap **Send a test message**. It arrives in Telegram.

From then on the bot answers only you. Anyone else, and any group, gets no
reply.

- Type, or send a voice note: Jarvis answers voice notes with one.
- `/new` starts a fresh conversation, `/status` shows approvals and the kill
  switch, and `/standdown` pauses everything Jarvis does on its own.
- Low- and medium-risk actions arrive with **Approve** and **Reject** buttons,
  and **Undo** while a send can still be undone. High-risk actions point you
  to the app and your passkey.
- Telegram chats aren't end-to-end encrypted. So Jarvis leaves your sensitive
  memories and your personal profile out of them, and masks anything that
  looks like a secret. [security.md](security.md) has the details.

`make doctor ONLINE=1` checks the token with Telegram.

---

## Step 8: The onboarding interview

Open **Getting to know you** (Onboarding). There are 12 short modules:

1. You
2. Your business
3. Ideal clients
4. Portfolio
5. Stacks
6. Design taste
7. Writing tone
8. Schedule
9. Goals
10. VIPs
11. Boundaries
12. Optional personal details

You answer by typing. You can stop anytime, and it picks up where you left
off. What you tell Jarvis elsewhere (in chat, on the Talk page or on Telegram)
teaches it too: it shows up on the "What I know" page for you to confirm.

When the required answers are at least 80% complete, review the summary on the
"What I know" page and **sign it off**. Until you do, Jarvis only talks and
drafts: nothing runs on its own. You can edit or delete anything it knows at
any time, and the change applies immediately.

---

## Step 9: The reboot test

Do this once, to prove Jarvis survives a power cut with nobody logged in:

1. Restart Windows and **don't log in**.
2. Wait 2 minutes, then open Jarvis on your phone (over mobile data, to be
   sure). It should load, and your chat history should be there.
3. Log in to Windows and run `make doctor` in Ubuntu. Everything should be ✔.

If Jarvis didn't come back, see "Jarvis doesn't come back after a reboot" in
[runbook.md](runbook.md).

You're set. Day-to-day commands, updates, backups and fixes are in
[runbook.md](runbook.md).
