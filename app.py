# app.py  — unified bot (config + job) with webhook
import os, json, logging
from datetime import datetime, date as _date
from typing import Dict, Any, List
import pytz
import telebot
import telebot.apihelper as apihelper
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ======== ENV ========
BOT_TOKEN      = os.environ["BOT_TOKEN"]            # BotFather token (tək bot)
GROUP_CHAT_ID  = int(os.environ.get("GROUP_CHAT_ID", "0"))  # Qrup id (job mesajları üçün)
ADMIN_PIN      = os.environ.get("BOT_ADMIN_PIN", "changeme")
TIMEZONE       = pytz.timezone(os.environ.get("TIMEZONE", "Asia/Baku"))

# ======== Paths ========
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(BASE_DIR, "config.json")
USERS_FILE     = os.path.join(BASE_DIR, "users.json")      # { "Ad": chat_id }
ANSWERS_FILE   = os.path.join(BASE_DIR, "answers.json")    # { "YYYY-MM-DD": { "Ad": "cavab" } }
ADMINS_FILE    = os.path.join(BASE_DIR, "admins.json")     # [chat_id, ...]

# ======== Bootstrap & utils ========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
apihelper.SESSION_TIME_TO_LIVE = 60

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_config_or_die() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"{CONFIG_FILE} tapılmadı.")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def today_str() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

def today_weekday() -> int:
    return datetime.now(TIMEZONE).isoweekday()  # 1=Mon .. 7=Sun

def _today_date() -> _date:
    return datetime.now(TIMEZONE).date()

def _parse_date(s: str) -> _date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def canon_name(raw: str, team: List[str]) -> str | None:
    for t in team:
        if t.lower() == raw.lower():
            return t
    return None

def parse_hhmm(s: str):
    hh, mm = [int(x) for x in s.split(":")]
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError
    return hh, mm

def is_admin(chat_id: int) -> bool:
    admins = load_json(ADMINS_FILE, [])
    return chat_id in admins

def add_admin(chat_id: int):
    admins = load_json(ADMINS_FILE, [])
    if chat_id not in admins:
        admins.append(chat_id)
        save_json(ADMINS_FILE, admins)

def get_remote_today() -> List[str]:
    wd = today_weekday()
    return [
        name for name, days in WEEKLY_SCHEDULE.items()
        if wd in days and not is_on_vacation(name)
    ]

def is_on_vacation(name: str, d: _date | None = None) -> bool:
    if d is None:
        d = _today_date()
    ranges = VACATIONS.get(name, [])
    for rng in ranges:
        try:
            start, end = _parse_date(rng[0]), _parse_date(rng[1])
            if start <= d <= end:
                return True
        except Exception:
            pass
    return False

def make_scrum_prompt() -> str:
    return (
        "Salam! Bu gün remote-san. Xahiş edirəm bu 3 suala qısa cavab yaz:\n"
        "1) Dünən nə etdin?\n"
        "2) Bu gün nə edəcəksən?\n"
        "3) Bloklayan problem varmı?\n"
        f"Qeyd: Cavabınızı saat {SUMMARY_HOUR:02d}:{SUMMARY_MINUTE:02d}-a kimi göndərin."
    )

# ======== Load config ========
CONFIG           = _load_config_or_die()
TEAM             = CONFIG["TEAM"]
WEEKLY_SCHEDULE  = CONFIG["WEEKLY_SCHEDULE"]
VACATIONS        = CONFIG.get("VACITIONS", CONFIG.get("VACATIONS", {}))
PROMPT_HOUR      = CONFIG["PROMPT_HOUR"]
PROMPT_MINUTE    = CONFIG["PROMPT_MINUTE"]
SUMMARY_HOUR     = CONFIG["SUMMARY_HOUR"]
SUMMARY_MINUTE   = CONFIG["SUMMARY_MINUTE"]
LIVE_SCRUM_AT    = CONFIG["LIVE_SCRUM_AT"]

TESTERS            = CONFIG.get("TESTERS", [])
TESTERS_PING_TIMES = CONFIG.get("TESTERS_PING_TIMES", [])

def reschedule_jobs():
    try:
        # Əsas iki job
        scheduler.add_job(
            job_send_prompts,
            CronTrigger(day_of_week="mon-fri", hour=PROMPT_HOUR, minute=PROMPT_MINUTE, timezone=TIMEZONE),
            id="prompt", replace_existing=True
        )
        scheduler.add_job(
            job_post_summary,
            CronTrigger(day_of_week="mon-fri", hour=SUMMARY_HOUR, minute=SUMMARY_MINUTE, timezone=TIMEZONE),
            id="summary", replace_existing=True
        )

        # 🔹 Köhnə testers_ping_* job-ları təmizlə
        for job in scheduler.get_jobs():
            if job.id.startswith("testers_ping_"):
                scheduler.remove_job(job.id)

        # 🔹 Configdə neçə vaxt varsa, o qədər job əlavə et
        for idx, t in enumerate(TESTERS_PING_TIMES):
            try:
                hour_str, minute_str = t.split(":")
                hour = int(hour_str)
                minute = int(minute_str)
            except Exception:
                # səhv formatlı saatları skip elə
                continue

            scheduler.add_job(
                job_testers_ping,
                CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=TIMEZONE),
                id=f"testers_ping_{idx}",
                replace_existing=True
            )

    except Exception:
        # burda çox səs-küy olmasın deyə pass edirdin, elə saxladım
        pass


# ======== Bot ========
bot = telebot.TeleBot(BOT_TOKEN)

# --- COMMON /start, /groupid, /job, /cfg_reload ---
@bot.message_handler(commands=['start'])
def cmd_start(message):
    if message.chat.type == "private":
        bot.reply_to(
            message,
            "Salam! Özünü qeyd etmək üçün /register <Ad> yaz.\n"
            f"Məsələn: /register Rza\nMövcud adlar: {', '.join(TEAM)}"
        )

@bot.message_handler(commands=['groupid'])
def cmd_groupid(message):
    bot.reply_to(message, f"Group chat id: {message.chat.id}")

@bot.message_handler(commands=['job'])
def cmd_job(message):
    today = today_str()
    remote = set(get_remote_today())
    lines = [f"📅 Bu gün ({today}) iş qrafiki:"]
    for member in TEAM:
        if is_on_vacation(member):
            mode = "🌴 Məzuniyyətdə"
        elif member in remote:
            mode = "🏠 Remote"
        else:
            mode = "🏢 Ofisdə"
        lines.append(f"• {member}: {mode}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=['cfg_reload'])
def cmd_cfg_reload(message):
    global CONFIG, TEAM, WEEKLY_SCHEDULE, VACATIONS
    global PROMPT_HOUR, PROMPT_MINUTE, SUMMARY_HOUR, SUMMARY_MINUTE, LIVE_SCRUM_AT
    global TESTERS, TESTERS_PING_TIMES   # 🔹 əlavə et

    try:
        CONFIG           = _load_config_or_die()
        TEAM             = CONFIG["TEAM"]
        WEEKLY_SCHEDULE  = CONFIG["WEEKLY_SCHEDULE"]
        VACATIONS        = CONFIG.get("VACITIONS", CONFIG.get("VACATIONS", {}))
        PROMPT_HOUR      = CONFIG["PROMPT_HOUR"]
        PROMPT_MINUTE    = CONFIG["PROMPT_MINUTE"]
        SUMMARY_HOUR     = CONFIG["SUMMARY_HOUR"]
        SUMMARY_MINUTE   = CONFIG["SUMMARY_MINUTE"]
        LIVE_SCRUM_AT    = CONFIG["LIVE_SCRUM_AT"]

        # 🔹 yenilər
        TESTERS            = CONFIG.get("TESTERS", [])
        TESTERS_PING_TIMES = CONFIG.get("TESTERS_PING_TIMES", [])

        reschedule_jobs()
        bot.reply_to(message, "✅ config.json yenidən yükləndi və cədvəllər yeniləndi.")
    except Exception as e:
        bot.reply_to(message, f"❌ Yükləmə alınmadı: {e}")


# --- REGISTER (DM) ---
@bot.message_handler(commands=['register'])
def cmd_register(message):
    if message.chat.type != "private":
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /register <Ad>\nMəs: /register Rza")
        return
    raw = parts[1].strip()
    canon = canon_name(raw, TEAM)
    if not canon:
        bot.reply_to(message, f"'{raw}' komandada tapılmadı. Mövcud adlar: {', '.join(TEAM)}")
        return
    users = load_json(USERS_FILE, {})
    users[canon] = message.chat.id
    save_json(USERS_FILE, users)
    bot.send_message(message.chat.id, f"Qeyd olundu ✅  {canon} → chat_id: {message.chat.id}")

# --- OPTIONAL: whoami (diagnostics). İstəsən silə bilərsən.
@bot.message_handler(commands=['whoami'])
def cmd_whoami(message):
    bot.reply_to(message, f"chat_id: {message.chat.id}")

# --- DM text = cavabların toplanması (komanda olmayan mətnlər) ---
@bot.message_handler(
    func=lambda m: m.chat.type == "private" and not (m.text or "").startswith("/"),
    content_types=['text']
)
def handle_private_text(message):
    users = load_json(USERS_FILE, {})
    name = next((k for k, v in users.items() if v == message.chat.id), None)
    if not name:
        bot.reply_to(message, "Zəhmət olmasa əvvəlcə /register <Ad> ilə qeydiyyatdan keç.")
        return

    bot.reply_to(
        message,
        "Təşəkkürlər! Cavabını qeyd etdim. ✅" if name in get_remote_today()
        else "Qeyd edildi. (Qeyd: bu gün remote siyahısında deyilsən.)"
    )

    answers = load_json(ANSWERS_FILE, {})
    today = today_str()
    answers.setdefault(today, {})
    answers[today][name] = (message.text or "").strip()
    save_json(ANSWERS_FILE, answers)

# ======== CONFIG COMMANDS (admin PIN) ========
def admin_only(fn):
    def wrapper(message, *a, **kw):
        if message.chat.type != "private":
            return
        if not is_admin(message.chat.id):
            bot.reply_to(message, "Bu əmri yerinə yetirmək üçün admin olmalısan. /auth <PIN>")
            return
        return fn(message, *a, **kw)
    return wrapper

@bot.message_handler(commands=['auth'])
def cmd_auth(message):
    if message.chat.type != "private":
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /auth <PIN>")
        return
    if parts[1].strip() == ADMIN_PIN:
        add_admin(message.chat.id)
        bot.reply_to(message, "✅ Admin təsdiqləndi.")
    else:
        bot.reply_to(message, "❌ Yanlış PIN.")

@bot.message_handler(commands=['cfg_show'])
@admin_only
def cmd_cfg_show(message):
    try:
        cfg = _load_config_or_die()

        # Səliqəli multi-line JSON
        text = json.dumps(
            cfg,
            ensure_ascii=False,   # Azərbaycan hərfləri üçün
            indent=2              # hər səviyyədə 2 boşluq
        )

        pretty = f"```json\n{text}\n```"

        # Config böyük deyil, limitə girmir, ona görə parçalamasız göndəririk
        bot.reply_to(message, pretty, parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(message, f"❌ Xəta: {e}")



@bot.message_handler(commands=['team_list'])
@admin_only
def cmd_team_list(message):
    cfg = _load_config_or_die()
    team = cfg.get("TEAM", [])
    bot.reply_to(message, ("👥 TEAM:\n- " + "\n- ".join(team)) if team else "TEAM boşdur.")

@bot.message_handler(commands=['team_add'])
@admin_only
def cmd_team_add(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /team_add <Ad>")
        return
    name = parts[1].strip()
    cfg = _load_config_or_die()
    team = cfg.get("TEAM", [])
    if canon_name(name, team):
        bot.reply_to(message, f"'{name}' artıq TEAM-də var.")
        return
    team.append(name); cfg["TEAM"] = team
    ws = cfg.get("WEEKLY_SCHEDULE", {}); ws.setdefault(name, []); cfg["WEEKLY_SCHEDULE"] = ws
    vac = cfg.get("VACATIONS", {});      vac.setdefault(name, []); cfg["VACATIONS"] = vac
    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"✅ '{name}' TEAM-ə əlavə edildi.")

@bot.message_handler(commands=['team_rm'])
@admin_only
def cmd_team_rm(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /team_rm <Ad>")
        return
    raw = parts[1].strip()
    cfg = _load_config_or_die()
    team = cfg.get("TEAM", [])
    name = canon_name(raw, team)
    if not name:
        bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
        return
    cfg["TEAM"] = [t for t in team if t != name]
    ws = cfg.get("WEEKLY_SCHEDULE", {}); ws.pop(name, None); cfg["WEEKLY_SCHEDULE"] = ws
    vac = cfg.get("VACATIONS", {});      vac.pop(name, None); cfg["VACATIONS"] = vac
    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"✅ '{name}' TEAM-dən silindi.")

@bot.message_handler(commands=['sched_show'])
@admin_only
def cmd_sched_show(message):
    parts = message.text.split(maxsplit=1)
    cfg = _load_config_or_die()
    ws = cfg.get("WEEKLY_SCHEDULE", {})
    if len(parts) == 1:
        if not ws:
            bot.reply_to(message, "WEEKLY_SCHEDULE boşdur.")
            return
        lines = ["📅 WEEKLY_SCHEDULE:"] + [f"- {k}: {ws[k]}" for k in ws]
        bot.reply_to(message, "\n".join(lines))
    else:
        raw = parts[1].strip()
        name = canon_name(raw, cfg.get("TEAM", []))
        if not name:
            bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
            return
        days = ws.get(name, [])
        bot.reply_to(message, f"{name}: {days if days else '—'}")

@bot.message_handler(commands=['sched_set'])
@admin_only
def cmd_sched_set(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "İstifadə: /sched_set <Ad> <günlər>  (Mon=1..Sun=7, misal: 1,3,5)")
        return
    raw = parts[1].strip()
    try:
        days = [int(x) for x in parts[2].replace(" ", "").split(",") if x]
        if any(d < 1 or d > 7 for d in days):
            raise ValueError
    except Exception:
        bot.reply_to(message, "Günlər 1..7 aralığında olmalıdır. Misal: 1,3,5")
        return
    cfg = _load_config_or_die()
    name = canon_name(raw, cfg.get("TEAM", []))
    if not name:
        bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
        return
    ws = cfg.get("WEEKLY_SCHEDULE", {}); ws[name] = days; cfg["WEEKLY_SCHEDULE"] = ws
    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"✅ {name} üçün günlər təyin edildi: {days}")

@bot.message_handler(commands=['vac_show'])
@admin_only
def cmd_vac_show(message):
    parts = message.text.split(maxsplit=1)
    cfg = _load_config_or_die(); vac = cfg.get("VACATIONS", {})
    if len(parts) == 1:
        if not vac:
            bot.reply_to(message, "VACATIONS boşdur.")
            return
        lines = ["🌴 VACATIONS:"] + [f"- {k}: {vac[k]}" for k in vac]
        bot.reply_to(message, "\n".join(lines))
    else:
        raw = parts[1].strip()
        name = canon_name(raw, cfg.get("TEAM", []))
        if not name:
            bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
            return
        bot.reply_to(message, f"{name}: {vac.get(name, []) or '—'}")

@bot.message_handler(commands=['vac_add'])
@admin_only
def cmd_vac_add(message):
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        bot.reply_to(message, "İstifadə: /vac_add <Ad> <YYYY-MM-DD> <YYYY-MM-DD>")
        return
    raw, a, b = parts[1].strip(), parts[2].strip(), parts[3].strip()
    _parse_date(a); _parse_date(b)  # validate
    cfg = _load_config_or_die()
    name = canon_name(raw, cfg.get("TEAM", []))
    if not name:
        bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
        return
    vac = cfg.get("VACATIONS", {}); vac.setdefault(name, []).append([a, b]); cfg["VACATIONS"] = vac
    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"✅ {name}: {a} → {b} əlavə edildi.")

@bot.message_handler(commands=['vac_rm'])
@admin_only
def cmd_vac_rm(message):
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        bot.reply_to(message, "İstifadə: /vac_rm <Ad> <YYYY-MM-DD> <YYYY-MM-DD>")
        return
    raw, a, b = parts[1].strip(), parts[2].strip(), parts[3].strip()
    cfg = _load_config_or_die()
    name = canon_name(raw, cfg.get("TEAM", []))
    if not name:
        bot.reply_to(message, f"'{raw}' TEAM-də tapılmadı.")
        return
    vac = cfg.get("VACATIONS", {})
    vac[name] = [rng for rng in vac.get(name, []) if rng != [a, b]]
    cfg["VACATIONS"] = vac
    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"✅ {name}: {a} → {b} silindi.")

@bot.message_handler(commands=['prompt'])
@admin_only
def cmd_prompt(message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /prompt HH:MM\nNümunə: /prompt 09:15")
        return

    tm = parts[1].strip()
    try:
        hh, mm = tm.split(":")
        hh = int(hh); mm = int(mm)
    except:
        bot.reply_to(message, f"❗ Yanlış vaxt formatı: `{tm}`. Düz format: HH:MM")
        return

    cfg = _load_config_or_die()
    cfg["PROMPT_HOUR"] = hh
    cfg["PROMPT_MINUTE"] = mm

    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"⏰ Prompt vaxtı yeniləndi: {hh:02d}:{mm:02d}\n💡 /cfg_reload yaz ki, dərhal tətbiq olunsun.")

@bot.message_handler(commands=['summary'])
@admin_only
def cmd_summary(message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /summary HH:MM\nNümunə: /summary 17:00")
        return

    tm = parts[1].strip()
    try:
        hh, mm = tm.split(":")
        hh = int(hh); mm = int(mm)
    except:
        bot.reply_to(message, f"❗ Yanlış format: `{tm}`. Düz format: HH:MM")
        return

    cfg = _load_config_or_die()
    cfg["SUMMARY_HOUR"] = hh
    cfg["SUMMARY_MINUTE"] = mm

    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"📌 Summary vaxtı yeniləndi: {hh:02d}:{mm:02d}\n💡 /cfg_reload yaz.")

@bot.message_handler(commands=['live'])
@admin_only
def cmd_live(message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(message, "İstifadə: /live HH:MM\nNümunə: /live 09:40")
        return

    tm = parts[1].strip()
    try:
        hh, mm = tm.split(":")
        hh = int(hh); mm = int(mm)
    except:
        bot.reply_to(message, f"❗ Yanlış format: `{tm}`. Düz format: HH:MM")
        return

    cfg = _load_config_or_die()
    cfg["LIVE_SCRUM_AT"] = f"{hh}:{mm}"

    save_json(CONFIG_FILE, cfg)
    bot.reply_to(message, f"🎥 Canlı scrum saatı yeniləndi: {hh:02d}:{mm:02d}\n💡 /cfg_reload yaz.")


@bot.message_handler(commands=['testping'])
@admin_only
def cmd_testping(message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(
            message,
            "İstifadə: /testping HH:MM[,HH:MM,...]\n"
            "Nümunə: /testping 11:00,17:30"
        )
        return

    # Mətni təmizlə (boşluqları sil)
    raw = parts[1].replace(" ", "")
    times = raw.split(",")

    # Validasiya
    for t in times:
        try:
            hh, mm = t.split(":")
            int(hh); int(mm)  # int çevirmə ilə yoxlayır
        except Exception:
            bot.reply_to(message, f"❗ Yanlış vaxt formatı: `{t}`. Düz format: HH:MM")
            return

    # Config-i yüklə
    cfg = _load_config_or_die()
    cfg["TESTERS_PING_TIMES"] = times

    # Config-i saxla (mövcud mexanizmlə)
    save_json(CONFIG_FILE, cfg)

    bot.reply_to(
        message,
        "✅ Tester ping saatları yeniləndi: " + ", ".join(times) +
        "\n💡 Dərhal effekt verməsi üçün: `/cfg_reload` yaz."
    )
 


# ======== Scheduled jobs (prompt & summary) ========
def job_send_prompts():
    users = load_json(USERS_FILE, {})
    active_team  = [m for m in TEAM if not is_on_vacation(m)]
    remote_today = [m for m in get_remote_today() if m in active_team]
    non_remote   = [m for m in active_team if m not in remote_today]
    sent = []
    for name in remote_today:
        chat_id = users.get(name)
        if chat_id:
            try:
                bot.send_message(chat_id, make_scrum_prompt())
                sent.append(name)
            except Exception as e:
                logging.exception("DM send error for %s: %s", name, e)
    if GROUP_CHAT_ID:
        bot.send_message(
            GROUP_CHAT_ID,
            f"🕘 Remote olanlara scrum sorğusu göndərildi: {', '.join(sent)}" if sent
            else "🕘 Bu gün remote siyahısı boşdur (scrum sorğusu göndərilmədi)."
        )
        if non_remote:
            bot.send_message(
                GROUP_CHAT_ID,
                f"📣 Remote olmayanlar üçün {LIVE_SCRUM_AT}-də live scrum: {', '.join(non_remote)}"
            )

def job_testers_ping():
    # Qrup konfiq olunmayıbsa / tester siyahısı boşdursa, heç nə etmə
    if not GROUP_CHAT_ID or not TESTERS:
        return

    names = ", ".join(TESTERS)
    text = f"{names}, zəhmət olmasa test etdiyiniz taskların cari statuslarını qeyd edin."

    try:
        bot.send_message(GROUP_CHAT_ID, text)
    except Exception as e:
        logging.exception("job_testers_ping ERROR: %s", e)


def job_post_summary():
    answers = load_json(ANSWERS_FILE, {})
    today = today_str()
    day_answers = answers.get(today, {})
    if GROUP_CHAT_ID:
        if day_answers:
            lines = [f"📋 {today} — Scrum cavabları:"]
            for k, v in day_answers.items():
                lines.append(f"• {k}: {v}")
            bot.send_message(GROUP_CHAT_ID, "\n".join(lines))
        else:
            bot.send_message(GROUP_CHAT_ID, f"📋 {today} üçün cavab yoxdur.")

# ======== FastAPI + webhook ========
app = FastAPI()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

@app.on_event("startup")
async def on_start():
    reschedule_jobs()
    scheduler.start()

@app.post("/hook")
async def hook(request: Request):
    update = await request.json()
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return PlainTextResponse("ok")

@app.get("/health")
async def health():
    return PlainTextResponse("ok")

# (Opsional) Platforma cron istifadə edəcəksə bu URL-ləri vura bilər:
@app.get("/cron/prompt")
async def cron_prompt():
    job_send_prompts()
    return JSONResponse({"status": "prompt_sent"})

@app.get("/cron/summary")
async def cron_summary():
    job_post_summary()
    return JSONResponse({"status": "summary_posted"})
from datetime import timezone as _tz

@bot.message_handler(commands=['sched_info'])
@admin_only
def cmd_sched_info(message):
    try:
        jobs = scheduler.get_jobs()
        if not jobs:
            bot.reply_to(message, "🕓 APScheduler: heç bir iş tapılmadı. (Ola bilər ki, Cron Job istifadə olunur və ya scheduler start olmayıb.)")
            return
        lines = ["🕓 APScheduler aktivdir. Mövcud işlər:"]
        for j in jobs:
            nrt = j.next_run_time
            if nrt is not None and nrt.tzinfo is not None:
                nrt_local = nrt.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            else:
                nrt_local = "—"
            lines.append(f"• {j.id}: növbəti icra = {nrt_local}")
        bot.reply_to(message, "\n".join(lines))
    except Exception as e:
        bot.reply_to(message, f"❌ Xəta: {e}")

# --- HELP (ümumi) ---
USER_HELP_TEXT = (
    "Əsas əmrlər:\n"
    "  /start – qısa tanışlıq\n"
    "  /register <Ad> – özünü qeydiyyatdan keçir\n"
    "  /job – bu günün ofis/remote/məzuniyyət xülasəsi\n"
    "  /groupid – qrupun chat_id-si\n"
)

@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.reply_to(message, USER_HELP_TEXT)

ADMIN_HELP_TEXT = (
    "Salam! Bu botla config.json-u redaktə edə bilərsən (yalnız DM).\n"
    "Öncə admin ol: /auth <PIN>\n"
    "\n"
    "TEAM:\n"
    "  /team_list\n"
    "  /team_add <Ad>\n"
    "  /team_rm <Ad>\n"
    "\n"
    "WEEKLY_SCHEDULE:\n"
    "  /sched_show [Ad]\n"
    "  /sched_set <Ad> <günlər>\n"
    "    (Mon=1..Sun=7, misal: 1,3,5)\n"
    "\n"
    "VACATIONS:\n"
    "  /vac_show [Ad]\n"
    "  /vac_add <Ad> <YYYY-MM-DD> <YYYY-MM-DD>\n"
    "  /vac_rm <Ad> <YYYY-MM-DD> <YYYY-MM-DD>\n"
    "\n"
    "Vaxtlar:\n"
    "  /prompt HH:MM        (standup xatırlatma vaxtı)\n"
    "  /summary HH:MM       (günün yekun vaxtı)\n"
    "  /live HH:MM          (canlı scrum vaxtı)\n"
    "  /testping HH:MM[,HH:MM,...]  (tester ping vaxtları)\n"
    "\n"
    "Bütöv konfiq:\n"
    "  /cfg_show\n"
    "\n"
    "Kodlar https://dashboard.render com/ hosting olunub\n"
    "Servis https://dash.cloudflare com/ ilə keep alive saxlanılır\n"
    "Qeyd: Dəyişiklikdən sonra botda /cfg_reload yaz ki, dərhal tətbiq olunsun."
)

@bot.message_handler(commands=['admin_help', 'help_admin'])
def cmd_admin_help(message):
    # yalnız DM-də göstər
    if message.chat.type != "private":
        return
    # yalnız admin görə bilsin
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bu siyahı üçün admin olmalısan. /auth <PIN>")
        return
    bot.reply_to(message, ADMIN_HELP_TEXT)
