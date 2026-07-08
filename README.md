# Daily History Notifier

This is a tiny GitHub Actions app that runs every day at 3pm America/New_York, finds an interesting historical event from Wikimedia's "On this day" feed, looks up related Spotify podcast shows, and sends a notification.

By default, the notification is a GitHub issue in this repository. If you watch the repo, GitHub will email you when the issue is created. You can also set `NOTIFY_WEBHOOK_URL` to send the same message to Slack, Discord, or another webhook endpoint.

## How it works

- `.github/workflows/daily-history.yml` runs on a schedule and via manual dispatch.
- `history_digest.py` checks that the current New York time is 3pm before sending.
- Wikimedia provides the historical events and Wikipedia links.
- Spotify is searched with the Client Credentials flow.
- The notification is sent as a GitHub issue unless `NOTIFY_WEBHOOK_URL` is configured.

## Setup

1. Create a new GitHub repository and push these files.
2. In the repo, go to **Settings -> Actions -> General -> Workflow permissions** and select **Read and write permissions**.
3. Create a Spotify app at <https://developer.spotify.com/dashboard>.
4. Add these repository secrets in **Settings -> Secrets and variables -> Actions**:

| Secret | Required | Purpose |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | Recommended | Spotify app client ID. |
| `SPOTIFY_CLIENT_SECRET` | Recommended | Spotify app client secret. |
| `OPENAI_API_KEY` | Optional | OpenAI API key. If set, OpenAI selects the most interesting event, chooses the best Wikipedia page, and ranks Spotify podcast matches. |
| `NOTIFY_WEBHOOK_URL` | Optional | Slack, Discord, or generic webhook URL. If omitted, the app creates a GitHub issue. |

`GITHUB_TOKEN` is provided automatically by GitHub Actions.

You can also set optional repository variables:

- `OPENAI_MODEL` chooses the OpenAI model used for event selection, Wikipedia page selection, and podcast ranking. If omitted, the workflow uses `gpt-4o-mini`.
- `EVENT_INTEREST_KEYWORDS` steers event selection toward preferred topics. If omitted, the workflow uses `war, crime, natural disasters, battles`.

To send notifications to Discord, open your Discord server and go to **Server Settings -> Integrations -> Webhooks**. Create a webhook for the channel you want, copy its URL, and save it as the `NOTIFY_WEBHOOK_URL` repository secret.

## Running manually

Use **Actions -> Daily history notification -> Run workflow**.

- `force=true` sends immediately even if it is not 3pm in New York.
- `date=MM-DD` lets you test a specific date, such as `07-20`.

## Local testing

```bash
FORCE_SEND=true HISTORY_DATE=07-20 python history_digest.py
```

Without GitHub or webhook credentials, the script prints the notification to the terminal.

## Notes

Spotify does not expose podcast ratings through its public API, so this searches for relevant shows and returns Spotify's search ranking. For stricter "highly rated" podcast ranking, you would need another data source that provides podcast reviews or ratings.
