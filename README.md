# SX Earning Bot (@sxearningbot)

Telegram referral and rewards bot built with Python, aiogram 3, and PostgreSQL. It uses direct referrals only; the referral reward can be changed by the admin.

## Features
- User dashboard, wallet, direct referral link, statistics, leaderboard
- Daily bonus (one claim per UTC calendar day)
- Admin-created tasks with manual approval
- Promo codes with usage limits and one claim per user
- UPI and bank-transfer withdrawal requests
- Manual admin approval/payment marking and refund on rejection
- Admin broadcast, ban/unban, analytics, logs
- PostgreSQL persistence

## Important before launch
This is a starter implementation. Test with a private test bot and small test rewards before inviting users. Referral signup rewards are credited when a new Telegram account first starts through a valid referral link; this proves the Telegram account started the bot, not that a person is unique. Do not describe this as strong identity verification. Add CAPTCHA/abuse review if needed.

The bot is not a payment processor. Admin must manually pay withdrawals using their bank/UPI app and then mark them paid. Follow applicable laws, tax rules, platform policies, and publish clear reward/withdrawal terms. Do not ask users to pay money to unlock earnings.

## 1. Create the bot
In Telegram, open @BotFather, create a bot, and keep its token secret. Do not paste the token into source code or public chat.

## 2. Find your Telegram numeric user ID
Use a reputable Telegram ID utility or a small private ID bot, then put the numeric ID in `ADMIN_IDS`. Never use a username in this setting. Verify the ID before deployment.

## 3. Deploy on Render
1. Put `bot.py`, `requirements.txt`, and `render.yaml` in a GitHub repository.
2. In Render, create a Blueprint from that repository and review the resources before applying. The YAML provisions a worker and a PostgreSQL database; the database plan/pricing/availability may vary, so select a currently available persistent database plan in the Render dashboard if needed.
3. Set `BOT_TOKEN` to your BotFather token.
4. Set `ADMIN_IDS` to your numeric Telegram ID (comma-separated for multiple admins).
5. Optionally set `SUPPORT_USERNAME`.
6. Deploy and check worker logs.

If Render rejects the database plan field in `render.yaml`, remove the `plan` line and choose an available PostgreSQL plan in the dashboard. Keep the database internal connection string in `DATABASE_URL`; do not post it publicly.

## 4. Admin commands
- `/admin` — open admin panel
- `/setreward 10` — set direct referral reward
- `/setdaily 2` — set daily bonus
- `/newpromo` — then send `CODE REWARD MAX_USES`, e.g. `WELCOME 5 100`
- `/addtask` — then send `Title | reward | description`
- `/deactivatetask TASK_ID`
- `/wdapprove WITHDRAWAL_ID` — approve a pending withdrawal
- `/wdpaid WITHDRAWAL_ID [REFERENCE]` — mark an approved withdrawal paid after you actually send it
- `/wdreject WITHDRAWAL_ID [reason]` — reject and refund
- `/taskapprove USER_ID TASK_ID`
- `/taskreject USER_ID TASK_ID`
- `/ban USER_ID`, `/unban USER_ID`
- `/user USER_ID`
- `/broadcast` — then send the message to broadcast
- `/cancel` — cancel a pending text form

## Withdrawal flow
A withdrawal request reserves the requested amount by deducting it from the user's available wallet. If the admin rejects it, the bot refunds it. If approved, admin should make the payment manually and use `/wdpaid`. Do not mark a withdrawal paid until the real transfer is complete.

## Security notes
- Keep `BOT_TOKEN`, `DATABASE_URL`, and admin IDs in Render environment variables.
- Enable two-factor authentication on the Telegram account that controls BotFather/admin.
- Use a private GitHub repository if preferred, but never commit secrets.
- Restrict admin IDs; commands check numeric Telegram IDs.
- Back up the database and test recovery.
- This starter stores submitted payout details in PostgreSQL; consider encryption and a retention/deletion policy before production use.
- Do not promise users guaranteed income. Set transparent reward rules and moderate fraud claims manually.

## Local run (optional)
Python 3.11+ recommended:
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set BOT_TOKEN, DATABASE_URL, ADMIN_IDS in your shell environment
python bot.py
```


## Render Web Service deployment (webhook mode)
This package uses an HTTP webhook endpoint for Render Web Services. Set `BOT_TOKEN`, `DATABASE_URL`, and `ADMIN_IDS` in Render. Set a strong `WEBHOOK_SECRET` containing only letters, numbers, `_` or `-`. Render's free database/service limits and availability can change; free hosting is not a guarantee of uninterrupted 24/7 operation, and free databases may expire.
