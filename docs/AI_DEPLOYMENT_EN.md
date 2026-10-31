# AI deployment and assistance guide

Read this guide and the [README](../README.md) before assisting with deployment. Set up the generic blank instance on the user's own Windows computer. Never reuse credentials, contact data, QQ login state, databases, or persona settings from the original developer's machine or conversation history.

## Privacy boundaries

- Do not read, print, copy, or upload the user's `.env`, `data/`, QQ login cache, QR codes, API keys, conversation records, or databases. When checking setup, report only whether required fields are present or missing.
- Do not ask the user to paste keys into an AI chat. Have them edit `.env` locally.
- Do not open firewall ports, expose services publicly, install a Windows service, migrate personal data, send bulk messages, delete user data, or stop unrelated bot processes.
- For troubleshooting, use the isolated tests in this repository or a new temporary directory. Never import a real user database into tests.
- Do not claim that real QQ login, message delivery, NapCat compatibility, or a security audit has been verified unless it was actually verified.

## Deployment steps

1. Confirm 64-bit Windows 10/11, Python 3.11+ with the `py` launcher, Git, and network access. The user installs and logs in to QQ and NapCat and manages their own third-party AI API account.
2. Clone the repository or download the GitHub ZIP. Extract it to a writable folder. Do not overwrite an existing personal bot folder.
3. Run `install.bat`. It creates `.venv`, installs dependencies, and copies `.env.example` to `.env` only if `.env` does not exist.
4. Ask the user to fill `MY_QQ`, `MASTER_QQ`, `SELF_NICK`, and `DEEPSEEK_API_KEY` in `.env` on their own computer. Do not enter, receive, or echo secrets. They may also set `ZHIPU_API_KEY`, `DEFAULT_CITY`, model endpoints, and NapCat endpoints.
5. The user logs in to QQ/NapCat and configures the OneBot v11 HTTP server and forward WebSocket server. Bind both to `127.0.0.1` only. Defaults are ports 3000 and 3001; keep `.env` endpoints synchronized if changed.
6. Run `start-qq-bot.bat`. The management UI binds to loopback. Do not forward its `#token=` URL, and do not expose the UI or OneBot endpoints through a reverse proxy, public port forward, or tunnel.
7. First check configuration and the management page without sending messages. Only test sending, manual replies, or scheduled tasks after the user explicitly selects their own test account/group and authorizes real messages.
8. Record what remains unverified. Explain third-party API charges and backups. Stop the bot before backing up private `data/`, `.env`, `persona.txt`, or other personal configuration; store backups encrypted.

## Management pages

- **Overview:** Inspect the connection state, recent activity, and upcoming tasks.
- **Administrator management:** Maintain QQ accounts with bot-management permissions. Grant access only to trusted people.
- **Contact management:** Maintain notes, nicknames, relationships, memories, and per-contact personas.
- **Scheduled tasks:** Create, edit, pause, resume, and delete reminders. Enabled tasks send messages on schedule.
- **Conversation management:** View local records, pause automatic replies for a conversation, or send a manual reply after administrator confirmation.
- **Persona and rules:** Edit the global persona. Per-contact settings live in contact profiles.
- **System settings:** Review connection summaries and the desktop script paths.

## Start and stop

Run `install.bat` → fill `.env` → start and log in to QQ/NapCat → run `start-qq-bot.bat`. Keep the console window open. `stop-qq-bot.bat` stops only the current project's `bot.py` process; it does not stop QQ/NapCat. Restart the bot after changing `.env`.

Automatic morning and daily digests are off by default. Deep-task and file-operation tools are disabled, and cross-user conversation search is administrator-only. OneBot token support is not implemented, so this release is not suitable for public Internet or shared-server deployment. Loopback binding for the UI does not replace OneBot authentication.

## Limitations and troubleshooting

- If QQ is not connected, the startup output and UI show the disconnected state. Do not send a test message to a real contact without the user's approval.
- API keys stay in the user's local `.env`; the management UI does not show key values.
- Scheduled tasks use the local system timezone and clock. Enabled tasks or digests may proactively message their configured recipient/group.
- If the target is Linux, a cloud server, a shared server, or requires public access, stop and reassess compatibility and authentication first. The current BAT scripts and OneBot settings are not a public deployment solution.
