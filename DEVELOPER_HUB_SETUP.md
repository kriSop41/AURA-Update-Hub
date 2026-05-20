# AURA Developer Hub Setup

## 1) Set a secure token on your server

Configure one of these environment variables:

- `AURA_DEV_HUB_TOKEN`
- `AURA_DEVELOPER_TOKEN`

If you do not set one, the server fallback token is `aura-dev-change-me` (only for local testing).

## 2) Open the hub

After deployment, open:

- `/developer-hub`

Example:

- `https://your-domain.com/developer-hub`

## 3) Publish updates

From the hub:

1. Enter developer token.
2. Enter `versionCode` (integer, must be higher than installed app version code).
3. Enter `versionName` (for example `1.8.0`).
4. Add update catalog ("what's new"), one line per item.
5. Upload a new `.apk` file (required for first publish, optional later).
6. Click **Publish Update**.

This writes/updates:

- `updates/latest_update.json`
- `updates/apk/<uploaded-file>.apk`

## 4) Android app behavior

On app launch, Android calls:

- `GET /api/app-update/latest`

If `latest_version_code` is higher than installed app version code, app shows an update popup with catalog.
When user taps update:

1. APK downloads via `DownloadManager`.
2. On completion, app opens installer intent.
3. If unknown-source install permission is required, app sends user to settings and resumes install after returning.

