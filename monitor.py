import json, logging, os, random, signal, threading, time
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("flights")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Секреты ---
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
TP_TOKEN = os.environ["TP_TOKEN"]
TP_MARKER = os.getenv("TP_MARKER", "")

# --- Параметры поиска ---
ORIGIN = os.getenv("ORIGIN", "MOW")
CURRENCY = os.getenv("CURRENCY", "rub")
# Бюджет на одного человека. BUDGET приоритетнее, MAX_PRICE оставлен для совместимости
BUDGET = float(os.getenv("BUDGET") or os.getenv("MAX_PRICE") or "40000")
MAX_VARIANTS = int(os.getenv("MAX_VARIANTS", "10"))
LIMIT = int(os.getenv("API_LIMIT", "1000"))

# --- Справочники для расшифровки кодов (можно дополнить через env) ---
BUILTIN_PLACES = {
    # Россия и СНГ
    "MOW": "Москва", "LED": "Санкт-Петербург", "KZN": "Казань", "SVX": "Екатеринбург",
    "OVB": "Новосибирск", "KRR": "Краснодар", "AER": "Сочи", "ROV": "Ростов-на-Дону",
    "KGD": "Калининград", "UFA": "Уфа", "KJA": "Красноярск", "VVO": "Владивосток",
    "MRV": "Мин. Воды", "GOJ": "Нижний Новгород", "KUF": "Самара", "VOG": "Волгоград",
    "MSQ": "Минск", "ALA": "Алматы", "TAS": "Ташкент",
    # Таиланд
    "BKK": "Бангкок", "HKT": "Пхукет", "USM": "Самуи", "CNX": "Чиангмай",
    "KBV": "Краби", "URT": "Сураттани", "UTP": "Паттайя (У-Тапао)", "HDY": "Хатъяй",
    "CEI": "Чианграй", "TST": "Транг",
    # Азия и другое
    "SIN": "Сингапур", "KUL": "Куала-Лумпур", "DPS": "Бали", "SGN": "Хошимин",
    "HAN": "Ханой", "CXR": "Нячанг", "PQC": "Фукуок", "HKG": "Гонконг",
    "CMB": "Коломбо", "GOI": "Гоа", "DEL": "Дели", "MLE": "Мале",
    "DXB": "Дубай", "AUH": "Абу-Даби", "DOH": "Доха", "IST": "Стамбул",
    "AMS": "Амстердам",
}
BUILTIN_AIRLINES = {
    "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "FV": "Россия",
    "UT": "ЮТэйр", "WZ": "Red Wings", "N4": "Nordwind", "Y7": "NordStar",
    "5N": "Smartavia", "A4": "Азимут",
    "TG": "Thai Airways", "FD": "Thai AirAsia", "AK": "AirAsia", "DD": "Nok Air",
    "VZ": "Thai Vietjet", "VJ": "Vietjet", "TR": "Scoot", "SQ": "Singapore Airlines",
    "MH": "Malaysia Airlines", "UL": "SriLankan Airlines",
    "G9": "Air Arabia", "FZ": "flydubai", "EK": "Emirates", "QR": "Qatar Airways",
    "EY": "Etihad", "TK": "Turkish Airlines", "PC": "Pegasus", "GF": "Gulf Air",
    "WY": "Oman Air", "KC": "Air Astana", "J2": "AZAL", "HY": "Uzbekistan Airways",
    "CZ": "China Southern", "MU": "China Eastern", "CA": "Air China",
    "HU": "Hainan Airlines", "3U": "Sichuan Airlines", "MF": "Xiamen Airlines",
    "ZH": "Shenzhen Airlines", "9C": "Spring Airlines", "SC": "Shandong Airlines",
    "CX": "Cathay Pacific", "KE": "Korean Air", "OZ": "Asiana", "BR": "EVA Air",
    "CI": "China Airlines", "AI": "Air India", "6E": "IndiGo",
}


def parse_map(raw: str) -> dict:
    out = {}
    for item in raw.split(","):
        k, sep, v = item.partition(":")
        if sep and k.strip() and v.strip():
            out[k.strip().upper()] = v.strip()
    return out


PLACES = {**BUILTIN_PLACES, **parse_map(os.getenv("NAMES", ""))}
AIRLINES = {**BUILTIN_AIRLINES, **parse_map(os.getenv("AIRLINE_NAMES", ""))}


def place_short(code: str) -> str:
    return PLACES.get(code.upper(), code)


def place(code: str) -> str:
    n = PLACES.get(code.upper())
    return f"{n} ({code})" if n else code


def airline_label(code) -> str:
    if not code:
        return "?"
    n = AIRLINES.get(str(code).upper())
    return f"{n} ({code})" if n else str(code)


CURRENCY_SIGNS = {"rub": "₽", "eur": "€", "usd": "$"}


def money(v) -> str:
    sign = CURRENCY_SIGNS.get(CURRENCY.lower(), CURRENCY.upper())
    return f"{round(v):,}".replace(",", "\u00a0") + f"\u00a0{sign}"


def days_word(n) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "дней"
    r = n % 10
    if r == 1:
        return "день"
    if 2 <= r <= 4:
        return "дня"
    return "дней"


def days_str(n) -> str:
    return f"{n} {days_word(n)}"


def months_in_range(d1: date, d2: date) -> list:
    if d1 > d2:
        return []
    y, m, out = d1.year, d1.month, []
    while (y, m) <= (d2.year, d2.month):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# Окно отпуска (включительно)
DATE_FROM = date.fromisoformat(os.environ["DATE_FROM"])
DATE_TO = date.fromisoformat(os.environ["DATE_TO"])
if DATE_FROM > DATE_TO:
    raise SystemExit("DATE_FROM must be <= DATE_TO")
WINDOW_DAYS = (DATE_TO - DATE_FROM).days + 1

# DATE_FROM == DATE_TO -> в одну сторону на эту дату; иначе туда-обратно в окне
ROUND_TRIP = DATE_FROM < DATE_TO
if ROUND_TRIP:
    _md = os.getenv("MIN_DAYS", "").strip()
    if not _md:
        raise SystemExit("Для окна DATE_FROM..DATE_TO задайте MIN_DAYS")
    MIN_DAYS = int(_md)
    if MIN_DAYS < 1:
        raise SystemExit("MIN_DAYS must be >= 1")
    if MIN_DAYS > WINDOW_DAYS:
        raise SystemExit("MIN_DAYS больше длины окна DATE_FROM..DATE_TO")
    last_dep = DATE_TO - timedelta(days=MIN_DAYS - 1)      # самый поздний возможный вылет
    first_ret = DATE_FROM + timedelta(days=MIN_DAYS - 1)   # самый ранний возможный возврат
    DEP_PERIODS = months_in_range(DATE_FROM, last_dep)
    RET_PERIODS = months_in_range(first_ret, DATE_TO)
else:
    MIN_DAYS = None
    DEP_PERIODS = [DATE_FROM.isoformat()]                  # конкретный день
    RET_PERIODS = [None]


def parse_limit(s: str, item: str):
    s = s.strip()
    if s == "":
        return None
    try:
        n = int(s)
    except ValueError:
        raise SystemExit(f"Некорректное число пересадок в {item!r}")
    if n < 0:
        raise SystemExit(f"Число пересадок не может быть отрицательным: {item!r}")
    return n


def parse_destinations(raw: str):
    """'HKT:1/2,BKK:1,CNX' -> [('HKT',1,2), ('BKK',1,1), ('CNX',None,None)]
    None = без ограничения. Одно число = и туда, и обратно."""
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        code, sep, spec = item.partition(":")
        code = code.strip().upper()
        if not code:
            raise SystemExit(f"Некорректный элемент DESTINATIONS: {item!r}")
        if sep:
            out_s, slash, back_s = spec.partition("/")
            max_out = parse_limit(out_s, item)
            max_back = parse_limit(back_s, item) if slash else max_out
        else:
            max_out = max_back = None
        out.append((code, max_out, max_back))
    if not out:
        raise SystemExit("DESTINATIONS пуст")
    return out


DESTINATIONS = parse_destinations(os.getenv("DESTINATIONS", "BKK:1/1,HKT:1/2"))

# --- Расписание ---
TZ_NAME = os.getenv("TZ", "UTC")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    raise SystemExit(f"Неизвестный часовой пояс TZ={TZ_NAME!r} (пример: Europe/Moscow)")


def parse_schedule(raw: str) -> list:
    """'8,12,16,20,24' или '8:30,13,20:15' -> отсортированный список времени. 24 = 00:00."""
    out = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        h_s, sep, m_s = item.partition(":")
        try:
            h = int(h_s)
            m = int(m_s) if sep else 0
        except ValueError:
            raise SystemExit(f"Некорректное время в SCHEDULE: {item!r}")
        if h == 24 and m == 0:
            h = 0
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise SystemExit(f"Время вне диапазона в SCHEDULE: {item!r}")
        out.add(dtime(h, m))
    return sorted(out)


SCHEDULE = parse_schedule(os.getenv("SCHEDULE", ""))
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() == "true"
RETRY_SEC = int(os.getenv("RETRY_SEC", "600"))             # повтор после сбоя
INTERVAL = int(os.getenv("INTERVAL_SEC", "3600"))          # если SCHEDULE пуст
JITTER = int(os.getenv("JITTER_SEC", "0"))                 # случайная добавка к ожиданию

# --- Поведение ---
SEEN_TTL = int(os.getenv("SEEN_TTL_SEC", str(7 * 86400)))
SITE = os.getenv("SITE_URL", "https://www.aviasales.ru")
NOTIFY_EMPTY = os.getenv("NOTIFY_EMPTY", "false").lower() == "true"
DEBUG_TICKET = os.getenv("DEBUG_TICKET", "false").lower() == "true"

STATE_FILE = Path(os.getenv("STATE_FILE", "/data/seen.json"))
HEARTBEAT = Path(os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat"))

stop = threading.Event()
session = requests.Session()
_empty_notified = False


def next_scheduled(now: datetime) -> datetime:
    for off in (0, 1, 2):
        day = now.date() + timedelta(days=off)
        for t in SCHEDULE:
            cand = datetime.combine(day, t, tzinfo=TZ)
            if cand > now:
                return cand
    raise RuntimeError("не удалось вычислить следующий запуск")


def sleep_until(target: datetime, healthy) -> None:
    """Ждём до target; пока всё хорошо - раз в минуту обновляем heartbeat."""
    while not stop.is_set():
        remaining = (target - datetime.now(TZ)).total_seconds()
        if remaining <= 0:
            return
        if healthy():
            HEARTBEAT.touch()
        stop.wait(min(60, remaining))


def load_seen() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if now - v["ts"] < SEEN_TTL}


def save_seen(seen: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen))
    tmp.replace(STATE_FILE)  # атомарная запись


def get_tickets(dest: str, dep_p: str, ret_p, max_out, max_back) -> list:
    direct = max_out == 0 and (not ROUND_TRIP or max_back == 0)
    params = {
        "origin": ORIGIN,
        "destination": dest,
        "departure_at": dep_p,
        "currency": CURRENCY,
        "sorting": "price",
        "direct": "true" if direct else "false",
        "one_way": "false" if ROUND_TRIP else "true",
        "unique": "false",
        "limit": LIMIT,
    }
    if ret_p:
        params["return_at"] = ret_p
    r = session.get(
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        params=params,
        headers={"X-Access-Token": TP_TOKEN},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]} | params={params}")
    body = r.json()
    if not body.get("success", True):
        raise RuntimeError(f"API error: {body}")
    return body.get("data", [])


def send_telegram(text: str) -> None:
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = session.post(url, json=payload, timeout=30)
    if r.status_code == 429:  # rate limit от Telegram
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
        r = session.post(url, json=payload, timeout=30)
    r.raise_for_status()


def build_link(t: dict) -> str:
    link = SITE + t["link"]
    if TP_MARKER:
        link += ("&" if "?" in link else "?") + f"marker={TP_MARKER}"
    return link


def fmt_d(s: str) -> str:
    return date.fromisoformat(s[:10]).strftime("%d.%m.%Y")


def fmt_dur(m):
    return f"{m // 60} ч {m % 60} мин" if isinstance(m, int) else None


def format_variant(i: int, is_new: bool, dest: str, t: dict) -> str:
    mark = " 🆕" if is_new else ""
    lines = [f"{i}. {place_short(ORIGIN)} → {place(dest)} — {money(t['price'])}{mark}"]

    if ROUND_TRIP:
        dep = date.fromisoformat(t["departure_at"][:10])
        ret = date.fromisoformat(t["return_at"][:10])
        n = (ret - dep).days + 1
        lines.append(f"📅 {fmt_d(t['departure_at'])} → {fmt_d(t['return_at'])} · "
                     f"{days_str(n)} отпуска")
        lines.append(f"🔁 Пересадок: туда {t.get('transfers', '?')} / "
                     f"обратно {t.get('return_transfers', '?')}")
    else:
        lines.append(f"📅 {fmt_d(t['departure_at'])}")
        lines.append(f"🔁 Пересадок: {t.get('transfers', '?')}")

    d_to, d_back = fmt_dur(t.get("duration_to")), fmt_dur(t.get("duration_back"))
    if ROUND_TRIP and d_to and d_back:
        lines.append(f"⏱ В пути: туда {d_to} / обратно {d_back}")
    else:
        d = fmt_dur(t.get("duration_to") or t.get("duration")) or "?"
        lines.append(f"⏱ В пути: {d}")

    lines.append(f"🛫 Авиакомпания: {airline_label(t.get('airline'))}")
    lines.append(build_link(t))
    return "\n".join(lines)


def limit_word(v) -> str:
    if v is None:
        return "без ограничений"
    if v == 0:
        return "0 (прямой)"
    return str(v)


def transfers_limit_text(max_out, max_back) -> str:
    if not ROUND_TRIP:
        return limit_word(max_out)
    if max_out == max_back:
        if max_out is None:
            return "без ограничений"
        if max_out == 0:
            return "0 — только прямые рейсы"
        return f"{max_out} (туда и обратно)"
    return f"туда {limit_word(max_out)} / обратно {limit_word(max_back)}"


def send_digest(top: list, new_keys: set) -> None:
    route = f"{place(ORIGIN)} → " + ", ".join(place(d) for d, _, _ in DESTINATIONS)
    lines = [f"✈️ {route}",
             f"Топ-{len(top)} по цене · бюджет до {money(BUDGET)}"]
    if ROUND_TRIP:
        lines.append(f"📅 Окно: {DATE_FROM:%d.%m.%Y}–{DATE_TO:%d.%m.%Y} "
                     f"({days_str(WINDOW_DAYS)})")
        lines.append(f"🏖 Отпуск: не менее {days_str(MIN_DAYS)}")
    else:
        lines.append(f"📅 Вылет {DATE_FROM:%d.%m.%Y}, в одну сторону")

    if len(DESTINATIONS) == 1:
        _, mo, mb = DESTINATIONS[0]
        lines.append(f"🔁 Макс. пересадок: {transfers_limit_text(mo, mb)}")
    else:
        lines.append("🔁 Макс. пересадок:")
        for d, mo, mb in DESTINATIONS:
            lines.append(f"   • {place_short(d)}: {transfers_limit_text(mo, mb)}")
    header = "\n".join(lines)

    blocks = [format_variant(i, key in new_keys, dest, t)
              for i, (price, key, dest, t) in enumerate(top, 1)]

    chunk = header
    for b in blocks:
        if len(chunk) + len(b) + 2 > 3800:     # лимит Telegram 4096 символов
            send_telegram(chunk)
            time.sleep(1)
            chunk = ""
        chunk = f"{chunk}\n\n{b}" if chunk else b
    if chunk:
        send_telegram(chunk)


def lim(v):
    return "∞" if v is None else str(v)


def run_once() -> None:
    global _empty_notified
    seen = load_seen()
    found = {}   # key -> (price, key, dest, ticket)
    debug_logged = False

    for dest, max_out, max_back in DESTINATIONS:
        for dep_p in DEP_PERIODS:
            for ret_p in RET_PERIODS:
                if ROUND_TRIP and ret_p < dep_p:     # 'YYYY-MM' сравнивается как строка
                    continue
                tickets = get_tickets(dest, dep_p, ret_p, max_out, max_back)
                if DEBUG_TICKET and tickets and not debug_logged:
                    log.info("raw ticket: %s", json.dumps(tickets[0], ensure_ascii=False))
                    debug_logged = True
                if len(tickets) >= LIMIT:
                    log.warning("%s %s/%s: ответ упёрся в лимит %d, часть билетов могла не попасть",
                                dest, dep_p, ret_p, LIMIT)
                in_window = skipped_tr = no_ret_field = 0

                for t in tickets:
                    dep = date.fromisoformat(t["departure_at"][:10])
                    if not (DATE_FROM <= dep <= DATE_TO):
                        continue

                    if ROUND_TRIP:
                        ret_s = (t.get("return_at") or "")[:10]
                        if not ret_s:
                            continue
                        ret = date.fromisoformat(ret_s)
                        if ret > DATE_TO or ret < dep:
                            continue
                        if (ret - dep).days + 1 < MIN_DAYS:      # дни включительно
                            continue
                    in_window += 1

                    tr = t.get("transfers")
                    if max_out is not None and tr is not None and tr > max_out:
                        skipped_tr += 1
                        continue

                    if ROUND_TRIP and max_back is not None:
                        rtr = t.get("return_transfers")
                        if rtr is None:
                            no_ret_field += 1          # поля нет: билет не отбрасываем
                        elif rtr > max_back:
                            skipped_tr += 1
                            continue

                    price = t["price"]
                    if price > BUDGET:
                        continue
                    key = f"{ORIGIN}-{dest}-{t['departure_at']}-{t.get('return_at', '')}"
                    if key not in found or price < found[key][0]:
                        found[key] = (price, key, dest, t)

                log.info("%s вылет %s%s (туда ≤ %s%s): подходит по датам %d, отсеяно по пересадкам %d",
                         dest, dep_p,
                         f", возврат {ret_p}" if ret_p else "",
                         lim(max_out),
                         f", обратно ≤ {lim(max_back)}" if ROUND_TRIP else "",
                         in_window, skipped_tr)
                if no_ret_field:
                    log.warning("%s: у %d билетов нет поля return_transfers, обратный лимит к ним не применён",
                                dest, no_ret_field)

    top = sorted(found.values(), key=lambda x: (x[0], x[3]["departure_at"]))[:MAX_VARIANTS]
    new_keys = {key for price, key, _, _ in top
                if key not in seen or price < seen[key]["price"]}

    if not top:
        log.info("Ничего не найдено по заданным критериям (даты, бюджет ≤ %s %s, пересадки)",
                 BUDGET, CURRENCY)
        if NOTIFY_EMPTY and not _empty_notified:
            send_telegram(f"🔍 Пока ничего не найдено в бюджете до {money(BUDGET)} "
                          "по заданным критериям. Напишу, как только появятся варианты.")
            _empty_notified = True
    elif new_keys:
        _empty_notified = False
        send_digest(top, new_keys)
        now = time.time()
        for price, key, _, _ in top:
            seen[key] = {"price": price, "ts": now}
        log.info("sent digest: %d вариантов, из них новых/подешевевших %d", len(top), len(new_keys))
    else:
        _empty_notified = False
        log.info("Новых вариантов нет, в топе те же %d, что уже отправлялись", len(top))
    save_seen(seen)


def main() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.touch()
    fails = 0

    if SCHEDULE:
        sched_s = ", ".join(t.strftime("%H:%M") for t in SCHEDULE) + f" ({TZ_NAME})"
    else:
        sched_s = f"каждые {INTERVAL} с"
    log.info("started: %s -> %s, окно %s..%s (%s), %s, топ-%d, бюджет ≤ %s %s",
             ORIGIN, DESTINATIONS, DATE_FROM, DATE_TO, days_str(WINDOW_DAYS),
             f"отпуск от {days_str(MIN_DAYS)}" if ROUND_TRIP else "в одну сторону",
             MAX_VARIANTS, BUDGET, CURRENCY)
    log.info("расписание: %s", sched_s)

    now = datetime.now(TZ)
    if RUN_ON_START or not SCHEDULE:
        next_at = now
    else:
        next_at = next_scheduled(now)
        log.info("первый запуск по расписанию: %s", next_at.strftime("%d.%m %H:%M"))

    while not stop.is_set():
        sleep_until(next_at, lambda: fails == 0)
        if stop.is_set():
            break

        try:
            run_once()
            HEARTBEAT.touch()
            if fails:
                log.info("recovered after %d failures", fails)
            fails = 0
        except Exception:
            fails += 1
            log.exception("run failed (%d in a row)", fails)
            if fails == 3:
                try:
                    send_telegram("⚠️ flights-monitor: 3 ошибки подряд, смотри логи")
                except Exception:
                    log.exception("failed to send alert")

        now = datetime.now(TZ)
        jitter = timedelta(seconds=random.uniform(0, JITTER)) if JITTER else timedelta(0)
        if SCHEDULE:
            next_at = next_scheduled(now) + jitter
        else:
            next_at = now + timedelta(seconds=INTERVAL) + jitter
        if 0 < fails < 3:                               # после сбоя пробуем раньше
            retry_at = now + timedelta(seconds=RETRY_SEC)
            if retry_at < next_at:
                next_at = retry_at
        log.info("следующий запуск: %s", next_at.strftime("%d.%m.%Y %H:%M:%S %Z"))

    log.info("stopped")


if __name__ == "__main__":
    main()