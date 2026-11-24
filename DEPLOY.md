# Deploying the Outreach Dashboard to Render

The hosted app has a login page, HTTPS, a persistent disk for your data, and connects Gmail with a
normal "Sign in with Google" redirect. Secrets are set in Render, never uploaded to GitHub.

## 1. Put the code on GitHub (private)
1. Install **GitHub Desktop** (desktop.github.com) and sign in.
2. **File → Add local repository →** choose this folder (`Outreach mails`). When it says it isn't a
   repository, click **create a repository** here. Name: `outreach-dashboard`. Leave *Initialize with README* off.
3. Check the file list on the left: it must **not** contain `.env`, `credentials`, `data`, `output`,
   `backups` or any `.db` file (`.gitignore` excludes them).
4. Write a summary such as "First version", click **Commit to main**, then **Publish repository**.
   Keep **"Keep this code private" ticked**.

Don't drag the folder onto github.com: the website upload ignores `.gitignore` and would upload your secrets.

## 2. Create the service on Render
1. Render dashboard → **New → Blueprint** → connect GitHub → pick `outreach-dashboard`.
2. Render reads `render.yaml` and asks for the secret values:
   - **APP_PASSWORD**: a long password you'll use to sign in.
   - **PUBLIC_BASE_URL**: leave as `https://example.com` for now; you'll fix it in step 3.
   - **GMAIL_CLIENT_SECRET_JSON**: leave `{}` for now (step 4).
   - **SENDER_EMAIL**: your Gmail address. **TEST_RECIPIENT**: your own address.
   - **CLAUDE_CODE_OAUTH_TOKEN**: leave empty for now (optional, step 7).
3. Click **Apply**. The first build takes a few minutes. Plan: Starter plus a 1 GB disk (paid).

## 3. Set the public address
1. Open the service and copy its address, e.g. `https://outreach-dashboard-abcd.onrender.com`.
2. **Environment** → set **PUBLIC_BASE_URL** to that address (no `/` at the end) → **Save changes**
   (Render redeploys).
3. Open the address, and you should see the **Sign in** page.

## 4. Google sign-in for the hosted app
1. console.cloud.google.com → the same project you use now → **APIs & Services → Credentials**.
2. **Create credentials → OAuth client ID → Web application**. Name: `Outreach hosted`.
3. **Authorized redirect URIs → Add URI**: `https://<your-address>/oauth2callback`
   (exactly your PUBLIC_BASE_URL + `/oauth2callback`). **Create**, then **Download JSON**.
4. **OAuth consent screen → Data access / Scopes**: make sure `gmail.send` and `gmail.readonly` are listed.
   Under **Audience / Test users**, your Gmail address must be listed.
5. In Render → **Environment** → **GMAIL_CLIENT_SECRET_JSON**: open the downloaded file in Notepad,
   copy **all** of it, paste it here → **Save changes**.

## 5. Move your data from this computer
1. On this computer: dashboard → **Settings → Backup & move data → Download backup (.db)**.
2. On the hosted app: sign in → **Settings → Backup & move data** → choose the file → type `REPLACE`
   → **Replace data with this backup**. Your contacts, drafts, sent history, replies and websites move over.

## 6. Connect Gmail on the hosted app
**Settings → Connect Gmail** → Google opens → choose your account → allow → you return to the app.
(If Google warns the app isn't verified: **Advanced → Go to … (unsafe)**.) Then **Follow-ups & replies →
Check replies now**.

**From now on use only the hosted app.** Stop the app on this computer (`Stop Outreach App.bat`) and
remove it from Windows startup. Two copies with separate databases could send duplicate emails.

## 7. Optional: "Find new websites with Claude" on the server
1. On this computer, in a terminal: `claude setup-token`, sign in, and copy the token it prints.
2. Render → Environment → **CLAUDE_CODE_OAUTH_TOKEN** → paste → Save.
   This uses your Claude subscription. The free Ahrefs DR lookup may not be available on the server, in
   which case DR stays "Not Checked" for sites found there.

## Updating later
Change code → GitHub Desktop → **Commit** → **Push origin**. Render redeploys automatically; your data
on the disk is kept. Download a backup from Settings now and then.
