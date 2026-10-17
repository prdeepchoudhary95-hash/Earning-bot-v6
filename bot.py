import asyncio
import logging
import os
import secrets
import string
from datetime import datetime, date, timezone
from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
BOT_NAME = os.getenv("BOT_NAME", "SX Earning Bot")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
MIN_WITHDRAWAL = Decimal(os.getenv("MIN_WITHDRAWAL", "100"))
CURRENCY = "₹"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sx-earning-bot")
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
pool: asyncpg.Pool | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT NOT NULL DEFAULT '',
  referrer_id BIGINT REFERENCES users(user_id),
  balance NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
  total_earned NUMERIC(12,2) NOT NULL DEFAULT 0,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_daily_bonus DATE,
  is_banned BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(user_id),
  kind TEXT NOT NULL,
  amount NUMERIC(12,2) NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS withdrawals (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(user_id),
  amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  method TEXT NOT NULL CHECK (method IN ('UPI','BANK')),
  details JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING','APPROVED','REJECTED','PAID')),
  admin_note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS tasks (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  reward NUMERIC(12,2) NOT NULL CHECK (reward >= 0),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS task_claims (
  user_id BIGINT NOT NULL REFERENCES users(user_id),
  task_id BIGINT NOT NULL REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(user_id, task_id)
);
CREATE TABLE IF NOT EXISTS promo_codes (
  code TEXT PRIMARY KEY,
  reward NUMERIC(12,2) NOT NULL CHECK (reward >= 0),
  max_uses INTEGER NOT NULL DEFAULT 100,
  uses INTEGER NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS promo_claims (
  user_id BIGINT NOT NULL REFERENCES users(user_id),
  code TEXT NOT NULL REFERENCES promo_codes(code),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(user_id, code)
);
CREATE TABLE IF NOT EXISTS admin_logs (
  id BIGSERIAL PRIMARY KEY,
  admin_id BIGINT NOT NULL,
  action TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, id DESC);
"""


def money(v) -> str:
    return f"{CURRENCY}{Decimal(str(v)):.2f}"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def setting(key: str, default: str) -> str:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await pool.execute(
        """INSERT INTO settings(key,value) VALUES($1,$2)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", key, value
    )


async def log_admin(admin_id: int, action: str):
    await pool.execute("INSERT INTO admin_logs(admin_id,action) VALUES($1,$2)", admin_id, action)


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 My Wallet", callback_data="wallet")
    b.button(text="👥 Refer & Earn", callback_data="refer")
    b.button(text="📊 Statistics", callback_data="stats")
    b.button(text="🎁 Daily Bonus", callback_data="daily")
    b.button(text="🎯 Tasks", callback_data="tasks")
    b.button(text="🏆 Leaderboard", callback_data="leaderboard")
    b.button(text="💸 Withdraw", callback_data="withdraw")
    b.button(text="🎟 Promo Code", callback_data="promo")
    b.button(text="📜 History", callback_data="history")
    b.button(text="📖 How to Earn", callback_data="how")
    b.button(text="🆘 Support", callback_data="support")
    b.adjust(2, 2, 2, 2, 2, 1)
    return b.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="menu")]])


def admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for label, data in [
        ("📊 Dashboard", "adm:dashboard"),
        ("👥 Manage User", "adm:userhelp"),
        ("💰 Referral Reward", "adm:rewardhelp"),
        ("💸 Withdrawals", "adm:withdrawals"),
        ("🎯 Tasks", "adm:tasks"),
        ("🎟 Promo Codes", "adm:promohelp"),
        ("📢 Broadcast", "adm:broadcasthelp"),
        ("🚫 Ban / Unban", "adm:banhelp"),
        ("⚙️ Settings", "adm:settings"),
        ("📜 Admin Logs", "adm:logs"),
    ]:
        b.button(text=label, callback_data=data)
    b.adjust(2, 2, 2, 2, 2)
    return b.as_markup()


async def get_user(user_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def ensure_user(tg_user, start_arg: str | None):
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", tg_user.id)
            if exists:
                await conn.execute(
                    "UPDATE users SET username=$2, first_name=$3 WHERE user_id=$1",
                    tg_user.id, tg_user.username, tg_user.first_name or ""
                )
                return False, None
            referrer = None
            if start_arg and start_arg.startswith("ref_"):
                try:
                    candidate = int(start_arg[4:])
                    if candidate != tg_user.id:
                        valid = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1 AND is_banned=FALSE", candidate)
                        if valid:
                            referrer = candidate
                except ValueError:
                    pass
            await conn.execute(
                """INSERT INTO users(user_id,username,first_name,referrer_id)
                   VALUES($1,$2,$3,$4)""",
                tg_user.id, tg_user.username, tg_user.first_name or "", referrer
            )
            reward = Decimal(await conn.fetchval("SELECT value FROM settings WHERE key='referral_reward'") or "10")
            if referrer and reward > 0:
                # The referral is credited exactly once, in the same DB transaction as user creation.
                await conn.execute(
                    "UPDATE users SET balance=balance+$2,total_earned=total_earned+$2 WHERE user_id=$1",
                    referrer, reward
                )
                await conn.execute(
                    "INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'REFERRAL',$2,$3)",
                    referrer, reward, f"Referral joined: {tg_user.id}"
                )
            return True, referrer


async def banned(user_id: int) -> bool:
    row = await get_user(user_id)
    return bool(row and row["is_banned"])


@dp.message(CommandStart())
async def start(message: Message):
    arg = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else None
    is_new, referrer = await ensure_user(message.from_user, arg)
    if await banned(message.from_user.id):
        await message.answer("🚫 Your account is currently restricted. Contact support if you think this is a mistake.")
        return
    user = await get_user(message.from_user.id)
    text = (
        f"💎 <b>{BOT_NAME}</b>\n\n"
        f"👋 Welcome, {message.from_user.first_name or 'User'}!\n"
        f"💰 Balance: <b>{money(user['balance'])}</b>\n"
        f"👥 Direct referrals: <b>{await pool.fetchval('SELECT COUNT(*) FROM users WHERE referrer_id=$1', message.from_user.id)}</b>\n\n"
        "Choose an option below:"
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")
    if is_new and referrer:
        try:
            await bot.send_message(referrer, f"🎉 A new user joined through your referral link. Reward: {money(await setting('referral_reward','10'))}")
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ You are not authorized.")
    await message.answer("👑 <b>Admin Panel</b>", reply_markup=admin_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "menu")
async def menu_cb(call: CallbackQuery):
    await call.answer()
    user = await get_user(call.from_user.id)
    if not user:
        return await call.message.answer("Please use /start first.")
    await call.message.answer(
        f"💎 <b>{BOT_NAME}</b>\n💰 Balance: <b>{money(user['balance'])}</b>\n\nChoose an option:",
        reply_markup=main_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "wallet")
async def wallet_cb(call: CallbackQuery):
    await call.answer()
    u = await get_user(call.from_user.id)
    if not u: return await call.message.answer("Please use /start first.")
    await call.message.answer(
        f"💰 <b>My Wallet</b>\n\nAvailable: <b>{money(u['balance'])}</b>\n"
        f"Lifetime earned: {money(u['total_earned'])}",
        reply_markup=back_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "refer")
async def refer_cb(call: CallbackQuery):
    await call.answer()
    me = await bot.get_me()
    count = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", call.from_user.id)
    reward = await setting("referral_reward", "10")
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    await call.message.answer(
        f"👥 <b>Refer & Earn</b>\n\nYour link:\n<code>{link}</code>\n\n"
        f"Direct referrals: <b>{count}</b>\nReward per new signup: <b>{money(reward)}</b>\n\n"
        "Share your link with friends. Each Telegram account can be counted only once.",
        reply_markup=back_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "stats")
async def stats_cb(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    refs = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", uid)
    earned = await pool.fetchval("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=$1 AND amount>0", uid)
    wd = await pool.fetchval("SELECT COUNT(*) FROM withdrawals WHERE user_id=$1", uid)
    await call.message.answer(
        f"📊 <b>Your Statistics</b>\n\nDirect referrals: {refs}\n"
        f"Total transaction credits: {money(earned)}\nWithdrawal requests: {wd}",
        reply_markup=back_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "daily")
async def daily_cb(call: CallbackQuery):
    await call.answer()
    reward = Decimal(await setting("daily_bonus", "2"))
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT last_daily_bonus,is_banned FROM users WHERE user_id=$1 FOR UPDATE", call.from_user.id)
            if not row:
                return await call.message.answer("Please use /start first.")
            if row["is_banned"]:
                return await call.message.answer("Your account is restricted.")
            today = datetime.now(timezone.utc).date()
            if row["last_daily_bonus"] == today:
                return await call.message.answer("🎁 Daily bonus already claimed today.", reply_markup=back_menu())
            await conn.execute("UPDATE users SET last_daily_bonus=$2,balance=balance+$3,total_earned=total_earned+$3 WHERE user_id=$1", call.from_user.id, today, reward)
            await conn.execute("INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'DAILY_BONUS',$2,'Daily bonus')", call.from_user.id, reward)
    await call.message.answer(f"🎉 You received {money(reward)} daily bonus!", reply_markup=back_menu())


@dp.callback_query(F.data == "leaderboard")
async def leaderboard_cb(call: CallbackQuery):
    await call.answer()
    rows = await pool.fetch(
        """SELECT u.first_name,u.username,COUNT(r.user_id) AS refs
           FROM users u LEFT JOIN users r ON r.referrer_id=u.user_id
           GROUP BY u.user_id ORDER BY refs DESC,u.joined_at ASC LIMIT 10"""
    )
    lines = ["🏆 <b>Top Referrers</b>\n"]
    for i, row in enumerate(rows, 1):
        name = row["first_name"] or (f"@{row['username']}" if row["username"] else "User")
        lines.append(f"{i}. {name}: {row['refs']} referrals")
    await call.message.answer("\n".join(lines), reply_markup=back_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "tasks")
async def tasks_cb(call: CallbackQuery):
    await call.answer()
    rows = await pool.fetch("SELECT * FROM tasks WHERE active=TRUE ORDER BY id DESC LIMIT 20")
    if not rows:
        return await call.message.answer("🎯 No tasks are available right now.", reply_markup=back_menu())
    b = InlineKeyboardBuilder()
    lines = ["🎯 <b>Available Tasks</b>\n"]
    for t in rows:
        lines.append(f"\n<b>#{t['id']} {t['title']}</b> — {money(t['reward'])}\n{t['description']}")
        b.button(text=f"Submit task #{t['id']}", callback_data=f"taskclaim:{t['id']}")
    b.button(text="🔙 Main Menu", callback_data="menu")
    b.adjust(1)
    await call.message.answer("\n".join(lines), reply_markup=b.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("taskclaim:"))
async def task_claim_cb(call: CallbackQuery):
    await call.answer()
    task_id = int(call.data.split(":")[1])
    task = await pool.fetchrow("SELECT * FROM tasks WHERE id=$1 AND active=TRUE", task_id)
    if not task:
        return await call.message.answer("Task is no longer available.")
    try:
        await pool.execute("INSERT INTO task_claims(user_id,task_id) VALUES($1,$2)", call.from_user.id, task_id)
    except asyncpg.UniqueViolationError:
        return await call.message.answer("You have already submitted this task.")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🎯 Task claim #{task_id} from user {call.from_user.id}.\nApprove with /taskapprove {call.from_user.id} {task_id} or reject with /taskreject {call.from_user.id} {task_id}")
        except Exception:
            pass
    await call.message.answer("✅ Task submitted for admin review.", reply_markup=back_menu())


@dp.callback_query(F.data == "withdraw")
async def withdraw_cb(call: CallbackQuery):
    await call.answer()
    b = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇳 UPI", callback_data="wdmethod:UPI")],
        [InlineKeyboardButton(text="🏦 Bank Transfer", callback_data="wdmethod:BANK")],
        [InlineKeyboardButton(text="📜 Withdrawal History", callback_data="wdhistory")],
        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="menu")]
    ])
    await call.message.answer("💸 Choose withdrawal method:", reply_markup=b)


@dp.callback_query(F.data.startswith("wdmethod:"))
async def wd_method_cb(call: CallbackQuery):
    await call.answer()
    method = call.data.split(":")[1]
    await pool.execute("UPDATE users SET username=username WHERE user_id=$1", call.from_user.id)
    # In-memory conversation state is acceptable only for temporary form input; balances remain in PostgreSQL.
    WD_STATE[call.from_user.id] = {"method": method, "step": "amount"}
    await call.message.answer(f"Enter withdrawal amount in rupees (minimum {money(MIN_WITHDRAWAL)}):")


@dp.callback_query(F.data == "wdhistory")
async def wd_history_cb(call: CallbackQuery):
    await call.answer()
    rows = await pool.fetch("SELECT id,amount,method,status,created_at FROM withdrawals WHERE user_id=$1 ORDER BY id DESC LIMIT 10", call.from_user.id)
    if not rows:
        return await call.message.answer("No withdrawal requests yet.", reply_markup=back_menu())
    lines = ["📜 <b>Withdrawal History</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']} • {money(r['amount'])} • {r['method']} • {r['status']} • {r['created_at'].strftime('%d-%m-%Y')}")
    await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=back_menu())


WD_STATE: dict[int, dict] = {}
PROMO_STATE: set[int] = set()
ADMIN_INPUT: dict[int, dict] = {}


@dp.message(F.text, ~F.text.startswith("/"))
async def text_router(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return
    if uid in WD_STATE:
        state = WD_STATE[uid]
        if state["step"] == "amount":
            try:
                amount = Decimal(text)
                if not amount.is_finite() or amount <= 0 or amount < MIN_WITHDRAWAL or amount.quantize(Decimal("0.01")) != amount:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                return await message.answer(f"Enter a valid amount, minimum {money(MIN_WITHDRAWAL)}.")
            user = await get_user(uid)
            if not user or user["balance"] < amount:
                WD_STATE.pop(uid, None)
                return await message.answer("Insufficient balance.", reply_markup=back_menu())
            state["amount"] = amount
            state["step"] = "details"
            if state["method"] == "UPI":
                return await message.answer("Enter your UPI ID (example: name@bank):")
            return await message.answer("Send bank details in this format:\nAccount holder name | Account number | IFSC code")
        if state["step"] == "details":
            method = state["method"]
            if method == "UPI":
                if len(text) > 100 or "@" not in text or " " in text:
                    return await message.answer("Please enter a valid UPI ID.")
                details = {"upi_id": text}
            else:
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or len(parts[1]) < 8 or len(parts[1]) > 20 or len(parts[2]) != 11:
                    return await message.answer("Invalid format. Use: Account holder name | Account number | IFSC code")
                details = {"account_holder": parts[0], "account_number": parts[1], "ifsc": parts[2].upper()}
            amount = state["amount"]
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("SELECT balance,is_banned FROM users WHERE user_id=$1 FOR UPDATE", uid)
                    if not row or row["is_banned"] or row["balance"] < amount:
                        WD_STATE.pop(uid, None)
                        return await message.answer("Could not submit request; check your balance/account status.")
                    await conn.execute("UPDATE users SET balance=balance-$2 WHERE user_id=$1", uid, amount)
                    await conn.execute("INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'WITHDRAWAL_HOLD',$2,$3)", uid, -amount, "Withdrawal request submitted")
                    wid = await conn.fetchval(
                        "INSERT INTO withdrawals(user_id,amount,method,details) VALUES($1,$2,$3,$4::jsonb) RETURNING id",
                        uid, amount, method, __import__("json").dumps(details)
                    )
            WD_STATE.pop(uid, None)
            await message.answer(f"✅ Withdrawal request #{wid} submitted for {money(amount)}. Amount is reserved while pending.", reply_markup=back_menu())
            for aid in ADMIN_IDS:
                try:
                    await bot.send_message(aid, f"💸 Withdrawal #{wid}\nUser: {uid}\nAmount: {money(amount)}\nMethod: {method}\nApprove: /wdapprove {wid}\nReject/refund: /wdreject {wid}")
                except Exception:
                    pass
            return

    if uid in PROMO_STATE:
        code = text.upper()
        async with pool.acquire() as conn:
            async with conn.transaction():
                promo = await conn.fetchrow("SELECT * FROM promo_codes WHERE code=$1 AND active=TRUE FOR UPDATE", code)
                if not promo or promo["uses"] >= promo["max_uses"]:
                    PROMO_STATE.discard(uid)
                    return await message.answer("❌ Promo code invalid or expired.", reply_markup=back_menu())
                try:
                    await conn.execute("INSERT INTO promo_claims(user_id,code) VALUES($1,$2)", uid, code)
                except asyncpg.UniqueViolationError:
                    PROMO_STATE.discard(uid)
                    return await message.answer("You have already used this code.", reply_markup=back_menu())
                await conn.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=$1", code)
                await conn.execute("UPDATE users SET balance=balance+$2,total_earned=total_earned+$2 WHERE user_id=$1", uid, promo["reward"])
                await conn.execute("INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'PROMO',$2,$3)", uid, promo["reward"], code)
        PROMO_STATE.discard(uid)
        return await message.answer(f"🎉 Promo applied. Added {money(promo['reward'])}.", reply_markup=back_menu())

    if uid in ADMIN_INPUT and is_admin(uid):
        state = ADMIN_INPUT[uid]
        ADMIN_INPUT.pop(uid, None)
        try:
            if state["action"] == "reward":
                value = Decimal(text)
                if value < 0 or value > 100000 or not value.is_finite(): raise ValueError
                await set_setting("referral_reward", str(value))
                await log_admin(uid, f"set referral_reward={value}")
                return await message.answer(f"Referral reward set to {money(value)}.")
            if state["action"] == "daily":
                value = Decimal(text)
                if value < 0 or value > 100000 or not value.is_finite(): raise ValueError
                await set_setting("daily_bonus", str(value))
                await log_admin(uid, f"set daily_bonus={value}")
                return await message.answer(f"Daily bonus set to {money(value)}.")
            if state["action"] == "ban":
                target = int(text.split()[0])
                await pool.execute("UPDATE users SET is_banned=TRUE WHERE user_id=$1", target)
                await log_admin(uid, f"banned {target}")
                return await message.answer(f"User {target} banned.")
            if state["action"] == "unban":
                target = int(text.split()[0])
                await pool.execute("UPDATE users SET is_banned=FALSE WHERE user_id=$1", target)
                await log_admin(uid, f"unbanned {target}")
                return await message.answer(f"User {target} unbanned.")
            if state["action"] == "promo":
                parts = text.split()
                if len(parts) != 3: raise ValueError
                code, reward, uses = parts[0].upper(), Decimal(parts[1]), int(parts[2])
                if reward < 0 or uses < 1 or uses > 100000: raise ValueError
                await pool.execute(
                    """INSERT INTO promo_codes(code,reward,max_uses) VALUES($1,$2,$3)
                       ON CONFLICT(code) DO UPDATE SET reward=EXCLUDED.reward,max_uses=EXCLUDED.max_uses,uses=0,active=TRUE""",
                    code, reward, uses
                )
                await log_admin(uid, f"created promo {code}")
                return await message.answer(f"Promo {code} created.")
            if state["action"] == "task":
                # Format: title | reward | description
                parts = [p.strip() for p in text.split("|", 2)]
                if len(parts) != 3 or not parts[0] or not parts[2]: raise ValueError
                reward = Decimal(parts[1])
                if reward < 0: raise ValueError
                tid = await pool.fetchval("INSERT INTO tasks(title,reward,description) VALUES($1,$2,$3) RETURNING id", parts[0], reward, parts[2])
                await log_admin(uid, f"created task {tid}")
                return await message.answer(f"Task #{tid} created.")
            if state["action"] == "broadcast":
                rows = await pool.fetch("SELECT user_id FROM users WHERE is_banned=FALSE")
                sent = 0
                for row in rows:
                    try:
                        await bot.send_message(row["user_id"], text)
                        sent += 1
                    except (TelegramForbiddenError, TelegramBadRequest):
                        pass
                    await asyncio.sleep(0.04)
                await log_admin(uid, f"broadcast sent={sent}")
                return await message.answer(f"Broadcast finished. Sent to {sent} users.")
        except (ValueError, InvalidOperation, asyncpg.PostgresError) as e:
            log.warning("Admin input failed: %s", e)
            return await message.answer("Invalid input or database error. Please check the format and try again.")
        return await message.answer("Unknown admin action.")

    if text.startswith("/"):
        return
    if text.lower() in {"cancel", "/cancel"}:
        WD_STATE.pop(uid, None)
        PROMO_STATE.discard(uid)
        ADMIN_INPUT.pop(uid, None)
        return await message.answer("Cancelled.", reply_markup=main_menu())


@dp.callback_query(F.data == "promo")
async def promo_cb(call: CallbackQuery):
    await call.answer()
    PROMO_STATE.add(call.from_user.id)
    await call.message.answer("🎟 Send your promo code:")


@dp.callback_query(F.data == "history")
async def history_cb(call: CallbackQuery):
    await call.answer()
    rows = await pool.fetch("SELECT kind,amount,note,created_at FROM transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 10", call.from_user.id)
    if not rows:
        return await call.message.answer("No transactions yet.", reply_markup=back_menu())
    lines = ["📜 <b>Recent Transactions</b>\n"]
    for r in rows:
        lines.append(f"{r['created_at'].strftime('%d-%m-%Y')} • {r['kind']} • {money(r['amount'])}\n{r['note']}")
    await call.message.answer("\n".join(lines), reply_markup=back_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "how")
async def how_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📖 <b>How to Earn</b>\n\n"
        "1. Share your unique referral link.\n"
        "2. Earn the configured reward when a new user joins through it.\n"
        "3. Claim the daily bonus once per day.\n"
        "4. Submit tasks and wait for admin review.\n"
        "5. Redeem a valid promo code.\n"
        "6. Request withdrawal after meeting the minimum balance.\n\n"
        "Rewards are subject to the published rules. Do not create fake accounts.",
        reply_markup=back_menu(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "support")
async def support_cb(call: CallbackQuery):
    await call.answer()
    text = f"🆘 Support: @{SUPPORT_USERNAME.lstrip('@')}" if SUPPORT_USERNAME else "🆘 Please contact the bot administrator for support."
    await call.message.answer(text, reply_markup=back_menu())


@dp.callback_query(F.data.startswith("adm:"))
async def admin_buttons(call: CallbackQuery):
    await call.answer()
    if not is_admin(call.from_user.id):
        return await call.message.answer("⛔ Not authorized.")
    key = call.data.split(":", 1)[1]
    if key == "dashboard":
        users = await pool.fetchval("SELECT COUNT(*) FROM users")
        balance = await pool.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        pending = await pool.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='PENDING'")
        refs = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        return await call.message.answer(f"📊 Dashboard\nUsers: {users}\nReferral joins: {refs}\nUser balances total: {money(balance)}\nPending withdrawals: {pending}", reply_markup=admin_menu())
    if key == "userhelp":
        return await call.message.answer("Use /user USER_ID to inspect a user. Use /ban USER_ID or /unban USER_ID.", reply_markup=admin_menu())
    if key == "rewardhelp":
        return await call.message.answer("Send /setreward AMOUNT to set the direct referral reward, e.g. /setreward 10\nSend /setdaily AMOUNT to set daily bonus.", reply_markup=admin_menu())
    if key == "withdrawals":
        rows = await pool.fetch("SELECT id,user_id,amount,method,status FROM withdrawals WHERE status='PENDING' ORDER BY id LIMIT 20")
        if not rows:
            return await call.message.answer("No pending withdrawals.", reply_markup=admin_menu())
        lines = ["💸 <b>Pending withdrawals</b>\n"]
        for r in rows:
            lines.append(f"#{r['id']} | user {r['user_id']} | {money(r['amount'])} | {r['method']} | {r['status']}\nApprove: /wdapprove {r['id']} • Reject/refund: /wdreject {r['id']}")
        return await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=admin_menu())
    if key == "tasks":
        return await call.message.answer("Create task: /addtask\nApprove task: /taskapprove USER_ID TASK_ID\nReject task: /taskreject USER_ID TASK_ID\nDeactivate task: /deactivatetask TASK_ID", reply_markup=admin_menu())
    if key == "promohelp":
        return await call.message.answer("Create promo by tapping the command and then send: /newpromo\nNext message format: CODE REWARD MAX_USES", reply_markup=admin_menu())
    if key == "broadcasthelp":
        return await call.message.answer("Send /broadcast, then send the message you want to broadcast.", reply_markup=admin_menu())
    if key == "banhelp":
        return await call.message.answer("Use /ban USER_ID or /unban USER_ID.", reply_markup=admin_menu())
    if key == "settings":
        return await call.message.answer(f"Referral reward: {money(await setting('referral_reward','10'))}\nDaily bonus: {money(await setting('daily_bonus','2'))}\nMinimum withdrawal: {money(MIN_WITHDRAWAL)}", reply_markup=admin_menu())
    if key == "logs":
        rows = await pool.fetch("SELECT admin_id,action,created_at FROM admin_logs ORDER BY id DESC LIMIT 10")
        lines = ["📜 <b>Recent admin actions</b>\n"] + [f"{r['created_at'].strftime('%d-%m %H:%M')} • {r['admin_id']} • {r['action']}" for r in rows]
        return await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=admin_menu())


# Admin command input states
@dp.message(Command("setreward"))
async def setreward_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        ADMIN_INPUT[message.from_user.id] = {"action": "reward"}
        await text_router(message.model_copy(update={"text": parts[1]}))
    else:
        ADMIN_INPUT[message.from_user.id] = {"action": "reward"}
        await message.answer("Send the new referral reward amount, e.g. 10.00")


@dp.message(Command("setdaily"))
async def setdaily_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        ADMIN_INPUT[message.from_user.id] = {"action": "daily"}
        await text_router(message.model_copy(update={"text": parts[1]}))
    else:
        ADMIN_INPUT[message.from_user.id] = {"action": "daily"}
        await message.answer("Send the new daily bonus amount.")


@dp.message(Command("ban"))
async def ban_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit(): return await message.answer("Usage: /ban USER_ID")
    await pool.execute("UPDATE users SET is_banned=TRUE WHERE user_id=$1", int(parts[1]))
    await log_admin(message.from_user.id, f"banned {parts[1]}")
    await message.answer("User banned.")


@dp.message(Command("unban"))
async def unban_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit(): return await message.answer("Usage: /unban USER_ID")
    await pool.execute("UPDATE users SET is_banned=FALSE WHERE user_id=$1", int(parts[1]))
    await log_admin(message.from_user.id, f"unbanned {parts[1]}")
    await message.answer("User unbanned.")


@dp.message(Command("wdapprove"))
async def wd_approve(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit(): return await message.answer("Usage: /wdapprove WITHDRAWAL_ID")
    wid = int(parts[1])
    row = await pool.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", wid)
    if not row or row["status"] != "PENDING": return await message.answer("Withdrawal not found or not pending.")
    await pool.execute("UPDATE withdrawals SET status='APPROVED',updated_at=NOW() WHERE id=$1 AND status='PENDING'", wid)
    await log_admin(message.from_user.id, f"approved withdrawal {wid}")
    await message.answer(f"Withdrawal #{wid} approved. Pay manually, then use /wdpaid {wid}.")


@dp.message(Command("wdpaid"))
async def wd_paid(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit(): return await message.answer("Usage: /wdpaid WITHDRAWAL_ID [REFERENCE]")
    wid = int(parts[1])
    row = await pool.fetchrow("UPDATE withdrawals SET status='PAID',admin_note=$2,updated_at=NOW() WHERE id=$1 AND status='APPROVED' RETURNING user_id,amount", wid, parts[2] if len(parts)>2 else "")
    if not row: return await message.answer("Withdrawal must be approved first.")
    await log_admin(message.from_user.id, f"marked withdrawal {wid} paid")
    try: await bot.send_message(row["user_id"], f"✅ Withdrawal #{wid} marked paid. Amount: {money(row['amount'])}.")
    except Exception: pass
    await message.answer("Marked as paid.")


@dp.message(Command("wdreject"))
async def wd_reject(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit(): return await message.answer("Usage: /wdreject WITHDRAWAL_ID [reason]")
    wid = int(parts[1])
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", wid)
            if not row or row["status"] not in ("PENDING", "APPROVED"):
                return await message.answer("Withdrawal not found or cannot be rejected.")
            await conn.execute("UPDATE withdrawals SET status='REJECTED',admin_note=$2,updated_at=NOW() WHERE id=$1", wid, parts[2] if len(parts)>2 else "")
            await conn.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["user_id"], row["amount"])
            await conn.execute("INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'WITHDRAWAL_REFUND',$2,$3)", row["user_id"], row["amount"], f"Refund for withdrawal #{wid}")
    await log_admin(message.from_user.id, f"rejected/refunded withdrawal {wid}")
    try: await bot.send_message(row["user_id"], f"↩️ Withdrawal #{wid} rejected. {money(row['amount'])} has been returned to your wallet.")
    except Exception: pass
    await message.answer("Rejected and refunded.")


@dp.message(Command("newpromo"))
async def newpromo_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    ADMIN_INPUT[message.from_user.id] = {"action": "promo"}
    await message.answer("Send: CODE REWARD MAX_USES\nExample: WELCOME 5 100")


@dp.message(Command("addtask"))
async def addtask_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    ADMIN_INPUT[message.from_user.id] = {"action": "task"}
    await message.answer("Send: Task title | reward | description")


@dp.message(Command("deactivatetask"))
async def deactivate_task(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit(): return await message.answer("Usage: /deactivatetask TASK_ID")
    await pool.execute("UPDATE tasks SET active=FALSE WHERE id=$1", int(parts[1]))
    await log_admin(message.from_user.id, f"deactivated task {parts[1]}")
    await message.answer("Task deactivated.")


@dp.message(Command("taskapprove"))
async def task_approve(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await message.answer("Usage: /taskapprove USER_ID TASK_ID")
    uid, tid = int(parts[1]), int(parts[2])
    async with pool.acquire() as conn:
        async with conn.transaction():
            claim = await conn.fetchrow("SELECT status FROM task_claims WHERE user_id=$1 AND task_id=$2 FOR UPDATE", uid, tid)
            task = await conn.fetchrow("SELECT reward FROM tasks WHERE id=$1", tid)
            if not claim or claim["status"] != "PENDING" or not task:
                return await message.answer("Pending task claim not found.")
            await conn.execute("UPDATE task_claims SET status='APPROVED' WHERE user_id=$1 AND task_id=$2", uid, tid)
            await conn.execute("UPDATE users SET balance=balance+$2,total_earned=total_earned+$2 WHERE user_id=$1", uid, task["reward"])
            await conn.execute("INSERT INTO transactions(user_id,kind,amount,note) VALUES($1,'TASK',$2,$3)", uid, task["reward"], f"Task #{tid} approved")
    await log_admin(message.from_user.id, f"approved task {tid} for {uid}")
    try: await bot.send_message(uid, f"🎉 Task #{tid} approved. Reward: {money(task['reward'])}.")
    except Exception: pass
    await message.answer("Task approved and reward credited.")


@dp.message(Command("taskreject"))
async def task_reject(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await message.answer("Usage: /taskreject USER_ID TASK_ID")
    await pool.execute("UPDATE task_claims SET status='REJECTED' WHERE user_id=$1 AND task_id=$2 AND status='PENDING'", int(parts[1]), int(parts[2]))
    await log_admin(message.from_user.id, f"rejected task {parts[2]} for {parts[1]}")
    await message.answer("Task claim rejected.")


@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    ADMIN_INPUT[message.from_user.id] = {"action": "broadcast"}
    await message.answer("Send the broadcast message now. Use /cancel to cancel.")


@dp.message(Command("user"))
async def user_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit(): return await message.answer("Usage: /user USER_ID")
    u = await get_user(int(parts[1]))
    if not u: return await message.answer("User not found.")
    refs = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", u["user_id"])
    await message.answer(f"User: {u['user_id']}\nName: {u['first_name']}\nBalance: {money(u['balance'])}\nReferrals: {refs}\nBanned: {u['is_banned']}")


async def main():
    """Run the Telegram bot through a webhook for Render Web Services."""
    global pool
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing. Deploy this app as a Render Web Service first.")

    webhook_secret = os.getenv("WEBHOOK_SECRET", "")
    if not webhook_secret:
        # Telegram webhook secret tokens may contain letters, digits, '_' and '-'.
        webhook_secret = secrets.token_urlsafe(32).replace(".", "_")
        log.warning("WEBHOOK_SECRET was not set; a temporary secret was generated. Set WEBHOOK_SECRET in Render for stable deployments.")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute("INSERT INTO settings(key,value) VALUES('referral_reward','10') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings(key,value) VALUES('daily_bonus','2') ON CONFLICT DO NOTHING")

    async def health(_request):
        return web.json_response({"ok": True, "service": "sxearningbot"})

    async def telegram_webhook(request):
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(supplied_secret, webhook_secret):
            raise web.HTTPUnauthorized(text="Invalid webhook secret")
        try:
            update = Update.model_validate(await request.json(), context={"bot": bot})
            await dp.feed_update(bot, update)
        except Exception:
            log.exception("Failed to process Telegram webhook update")
            raise web.HTTPBadRequest(text="Invalid update")
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(f"/telegram/{webhook_secret}", telegram_webhook)
    webhook_url = f"{external_url}/telegram/{webhook_secret}"

    runner = web.AppRunner(app)
    try:
        await bot.set_webhook(webhook_url, secret_token=webhook_secret, allowed_updates=dp.resolve_used_update_types())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
        await site.start()
        log.info("Webhook server started; health endpoint is available at %s/", external_url)
        # Keep the worker alive while aiohttp serves incoming webhook requests.
        await asyncio.Event().wait()
    finally:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            log.exception("Could not delete webhook during shutdown")
        await runner.cleanup()
        if pool is not None:
            await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
