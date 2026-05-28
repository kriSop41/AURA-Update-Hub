# AURA Update Hub

Static GitHub Pages update manifest for the AURA Music Android app.

## Public Files

Only these files need to be public:

```text
index.html
latest.json
logo.png
```

APK files should be uploaded to GitHub Releases, not committed directly to this repo.

## Publish An Update

1. Build the new APK.
2. Create a GitHub Release, for example `v1.8.3`.
3. Upload the APK to that release.
4. Copy the APK download URL.
5. Edit `latest.json`.
6. Commit and push.

Example:

```json
{
  "available": true,
  "latest_version_code": 50,
  "latest_version_name": "1.8.3",
  "apk_url": "https://github.com/USER/REPO/releases/download/v1.8.3/AURA.apk",
  "catalog": [
    "Moved updates to GitHub Pages",
    "Moved music API to Vercel"
  ],
  "force_update": false,
  "published_at": "2026-05-28T00:00:00Z"
}
```

The Android app checks:

```text
https://krisop41.github.io/AURA-Update-Hub/latest.json
```
