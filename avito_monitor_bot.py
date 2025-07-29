
# avito_monitor_bot_full_v3_fix.py
# -*- coding: utf-8 -*-
"""
v3-fix: как v3, но:
- экранирование HTML в сообщении для админа (имя/username), чтобы избежать "Unsupported start tag"
- все многострочные тексты в одной строке с \n (устойчиво к редакторам на Windows)
"""
import os, re, time, asyncio, html
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Deque, Callable
from collections import deque
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from background import keep_alive


import aiohttp
from bs4 import BeautifulSoup

FRESH_WINDOW_SEC = int(os.getenv("FRESH_WINDOW_SEC", "180"))
PRIME_ON_START = os.getenv("PRIME_ON_START", "1") == "1"
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

@dataclass
class SubscriberFilter:
    keywords_all: List[str] = field(default_factory=list)
    keywords_any: List[str] = field(default_factory=list)
    price_min: Optional[int] = None
    price_max: Optional[int] = None

@dataclass
class Subscription:
    id: int
    user_id: int
    search_key: str
    url: str
    flt: SubscriberFilter = field(default_factory=SubscriberFilter)

def _normalize_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    u = urlparse(url.strip())
    if not u.scheme: u = u._replace(scheme="https")
    netloc = u.netloc or "www.avito.ru"
    keep = {}
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        if k.lower().startswith("utm_"): continue
        keep[k] = v
    query = urlencode(sorted(keep.items()))
    u2 = u._replace(netloc=netloc, query=query)
    return urlunparse(u2)

def search_key_from_url(url: str) -> str:
    return _normalize_url(url)

_MONTHS_RU = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,"июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
def _parse_avito_date(date_str: str):
    if not date_str: return None
    s = date_str.strip().lower(); now = datetime.now()
    if "только что" in s: return now.timestamp()
    m = re.search(r"(\d+)\s*минут", s)
    if m: return (now - timedelta(minutes=int(m.group(1)))).timestamp()
    if "минуту назад" in s: return (now - timedelta(minutes=1)).timestamp()
    m = re.search(r"(\d+)\s*час", s)
    if m: return (now - timedelta(hours=int(m.group(1)))).timestamp()
    if "час назад" in s: return (now - timedelta(hours=1)).timestamp()
    m = re.search(r"(сегодня|вчера)[,\s]+(\d{1,2}:\d{2})", s)
    if m:
        day = now.date() if m.group(1)=="сегодня" else (now - timedelta(days=1)).date()
        hh, mm = map(int, m.group(2).split(":"))
        return datetime(day.year, day.month, day.day, hh, mm).timestamp()
    m = re.search(r"(\d{1,2})\s+([а-я]+)[,\s]+(\d{1,2}:\d{2})", s)
    if m:
        dd = int(m.group(1)); mon = _MONTHS_RU.get(m.group(2)); hh, mm = map(int, m.group(3).split(":"))
        if mon: return datetime(datetime.now().year, mon, dd, hh, mm).timestamp()
    return None

@dataclass
class Ad:
    ad_id: str
    url: str
    title: str = ""
    price: Optional[int] = None
    location: str = ""
    date_str: str = ""
    published_ts: Optional[float] = None
    description: str = ""

class Watcher:
    def __init__(self, search_key: str, url: str, bot: Bot, on_deliver=None):
        self.search_key = search_key; self.url = url; self.bot = bot
        self.subscribers: Dict[int, Subscription] = {}; self.task = None; self.seen: Dict[str, float] = {}
        self.interval_min = 0.30; self.interval_max = 0.85; self._interval = 0.45
        self._session: Optional[aiohttp.ClientSession] = None
        self.on_deliver = on_deliver

    async def start(self):
        if self.task and not self.task.done(): return
        if PRIME_ON_START: await self._prime_seen(20)
        self.task = asyncio.create_task(self._run(), name=f"watch:{self.search_key}")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
            self.task = None
        if self._session: await self._session.close(); self._session = None

    def has_subscribers(self): return bool(self.subscribers)
    def add_sub(self, sub: Subscription): self.subscribers[sub.user_id] = sub
    def remove_sub(self, user_id: int): self.subscribers.pop(user_id, None)

    async def _ensure_session(self):
        if self._session and not self._session.closed: return self._session
        self._session = aiohttp.ClientSession(); return self._session

    async def _fetch(self):
        session = await self._ensure_session()
        headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36","Accept-Language":"ru,en;q=0.9","Cache-Control":"no-cache"}
        try:
            async with session.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                if r.status==200: return await r.text()
        except Exception:
            return None
        return None

    async def _prime_seen(self, limit=20):
        html_text = await self._fetch()
        if not html_text: return
        for ad in self._parse_top(html_text, limit): self.seen[ad.ad_id] = time.time()

    def _parse_top(self, html_text: str, limit=20):
        soup = BeautifulSoup(html_text, "html.parser")
        root = soup.find("div", {"data-marker":"catalog-serp"})
        if not root: return []
        ads: List[Ad] = []
        for item in root.find_all("div", {"data-marker":"item"}, limit=limit):
            a = item.find("a", {"data-marker":"item-title"})
            if not a:
                continue

            href = a.get("href") or ""
            url = ("https://www.avito.ru"+href) if href.startswith("/") else href
            m = re.search(r"/(\d{7,})", href or ""); ad_id = (m.group(1) if m else url)
            title = a.get_text(strip=True)
            pr = item.find("meta", {"itemprop":"price"}); price = None
            if pr and pr.get("content","").isdigit():
                try: price = int(pr["content"])
                except: price = None
            date_tag = item.find("p", {"data-marker":"item-date"}); date_str = date_tag.get_text(strip=True) if date_tag else ""
            published_ts = _parse_avito_date(date_str)
            geo_div = item.select_one("div[class^=geo-root-]"); location = geo_div.get_text(strip=True) if geo_div else ""
            desc_meta = item.find("meta", {"itemprop":"description"}); description = desc_meta.get("content","").strip() if desc_meta else ""
            ads.append(Ad(ad_id=ad_id, url=url, title=title, price=price, location=location, date_str=date_str, published_ts=published_ts, description=description))
        return ads

    def _ad_passes_filters(self, ad: Ad, sub: Subscription) -> bool:
        flt = sub.flt
        if flt.price_min is not None and (ad.price is None or ad.price < flt.price_min): return False
        if flt.price_max is not None and (ad.price is None or ad.price > flt.price_max): return False
        t = f"{ad.title}\n{ad.description}".lower()
        return all(w.lower() in t for w in flt.keywords_all) if flt.keywords_all else True

    def _bump_interval(self, found_new: bool):
        self._interval = max(self.interval_min, self._interval*0.7) if found_new else min(self.interval_max, self._interval*1.1)

    def _cleanup_seen(self, ttl=7*24*3600):
        now = time.time()
        if len(self.seen)>20000:
            items = sorted(self.seen.items(), key=lambda kv: kv[1])
            for k,_ in items[:len(items)//2]: self.seen.pop(k, None)
        for k, ts in list(self.seen.items()):
            if now - ts > ttl: self.seen.pop(k, None)

    async def _run(self):
        while self.has_subscribers():
            found_new = False
            html_text = await self._fetch()
            if html_text:
                for ad in self._parse_top(html_text, limit=12):
                    if ad.ad_id in self.seen: continue
                    now_ts = time.time()
                    if ad.published_ts is not None and (now_ts - ad.published_ts) > FRESH_WINDOW_SEC:
                        self.seen[ad.ad_id] = now_ts; continue
                    self.seen[ad.ad_id] = now_ts
                    for sub in list(self.subscribers.values()):
                        if not self._ad_passes_filters(ad, sub): continue
                        lines = [f"<b>{ad.title}</b>"]
                        if ad.price is not None: lines.append(f"Цена: {ad.price} ₽")
                        if ad.location: lines.append(f"Город: {ad.location}")
                        if ad.date_str: lines.append(f"Дата: {ad.date_str}")
                        if ad.description: lines.append("\n"+ad.description)
                        lines.append(f"\n<a href='{ad.url}'>Открыть объявление</a>")
                        try:
                            await self.bot.send_message(sub.user_id, "\n".join(lines), disable_web_page_preview=True)
                            found_new = True
                            if self.on_deliver: self.on_deliver(sub.user_id, ad)
                        except Exception:
                            pass
            self._cleanup_seen(); self._bump_interval(found_new); await asyncio.sleep(max(0.25, self._interval))

@dataclass
class FeedItem:
    ts: float; title: str; price: Optional[int]; url: str; date_str: str

class WatcherManager:
    def __init__(self, bot: Bot):
        self.bot = bot; self.watchers: Dict[str, Watcher] = {}; self.subs_by_user: Dict[int, List[Subscription]] = {}; self._sub_id_seq = 1
        self.feed: Dict[int, Deque[FeedItem]] = {}
    def list_user_subs(self, user_id: int): return self.subs_by_user.get(user_id, [])
    def _next_sub_id(self): v=self._sub_id_seq; self._sub_id_seq+=1; return v
    def _on_deliver(self, user_id: int, ad: Ad):
        d = self.feed.setdefault(user_id, deque(maxlen=50))
        d.appendleft(FeedItem(ts=time.time(), title=ad.title, price=ad.price, url=ad.url, date_str=ad.date_str))
    async def add_subscription(self, user_id: int, url: str, flt: Optional[SubscriberFilter]=None) -> Subscription:
        if flt is None: flt = SubscriberFilter()
        key = search_key_from_url(url)
        sub = Subscription(id=self._next_sub_id(), user_id=user_id, search_key=key, url=url, flt=flt)
        w = self.watchers.get(key)
        if not w:
            w = Watcher(search_key=key, url=url, bot=self.bot, on_deliver=self._on_deliver); self.watchers[key]=w; await w.start()
        w.add_sub(sub); self.subs_by_user.setdefault(user_id, []).append(sub); return sub
    async def remove_subscription(self, user_id: int, sub_id: int) -> bool:
        subs = self.subs_by_user.get(user_id, [])
        for i, sub in enumerate(subs):
            if sub.id == sub_id:
                w = self.watchers.get(sub.search_key)
                if w:
                    w.remove_sub(user_id)
                    if not w.has_subscribers(): await w.stop(); self.watchers.pop(sub.search_key, None)
                subs.pop(i); return True
        return False
    def recent_feed(self, user_id: int, limit=10) -> List[FeedItem]:
        return list(self.feed.get(user_id, deque()))[:limit]

MAIN_KB = types.ReplyKeyboardMarkup(
    keyboard=[[types.KeyboardButton(text="📘 Инструкция"), types.KeyboardButton(text="🧭 Поиски")],
              [types.KeyboardButton(text="🛟 Поддержка"), types.KeyboardButton(text="🗂 Лента")]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Выберите пункт меню…",
)

class SearchWizard(StatesGroup):
    region = State(); category = State(); keywords = State(); price_min = State(); price_max = State(); sort_date = State(); confirm = State()

wizard_router = Router(name="wizard")

@wizard_router.message(F.text.in_(["🧭 Поиски", "Поиски", "/newsearch"]))
async def wizard_start(message: types.Message, state: FSMContext):
    await state.clear(); await state.set_state(SearchWizard.region)
    kb = types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="moskva"), types.KeyboardButton(text="sankt-peterburg")],
                                             [types.KeyboardButton(text="rossiya"), types.KeyboardButton(text="Другой регион")],
                                             [types.KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer("Мастер поиска: выберите регион (slug Avito, например: <code>moskva</code>)\nМожно нажать готовую кнопку или ввести свой.\n\nНапишите <b>Отмена</b> чтобы выйти.", reply_markup=kb)

@wizard_router.message(SearchWizard.region, F.text.casefold() == "отмена")
async def wizard_cancel_r(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.region)
async def wizard_got_r(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() == "другой регион":
        await message.answer("Введите slug региона вручную (например, <code>ekaterinburg</code>):"); return
    await state.update_data(region=text); await state.set_state(SearchWizard.category)
    kb = types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="telefony"), types.KeyboardButton(text="bytovaya_tehnika")],
                                             [types.KeyboardButton(text="kvartiry"), types.KeyboardButton(text="avtomobili")],
                                             [types.KeyboardButton(text="Другая категория")], [types.KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer("Категория (slug Avito). Можно выбрать из быстрых вариантов или ввести свою.", reply_markup=kb)

@wizard_router.message(SearchWizard.category, F.text.casefold() == "отмена")
async def wizard_cancel_c(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.category)
async def wizard_got_c(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() == "другая категория":
        await message.answer("Введите slug категории вручную (например, <code>telefony</code>):"); return
    await state.update_data(category=text); await state.set_state(SearchWizard.keywords)
    await message.answer("Ключевые слова (через запятую) или «-»:", reply_markup=types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="-")],[types.KeyboardButton(text="Отмена")]], resize_keyboard=True))

@wizard_router.message(SearchWizard.keywords, F.text.casefold() == "отмена")
async def wizard_cancel_k(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.keywords)
async def wizard_got_k(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    kws = [] if raw == "-" else [w.strip() for w in raw.replace(";", ",").replace("|", ",").split(",") if w.strip()]
    await state.update_data(keywords=kws); await state.set_state(SearchWizard.price_min)
    await message.answer("Минимальная цена (или «-»):", reply_markup=types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="-")],[types.KeyboardButton(text="Отмена")]], resize_keyboard=True))

@wizard_router.message(SearchWizard.price_min, F.text.casefold() == "отмена")
async def wizard_cancel_min(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.price_min)
async def wizard_got_min(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip(); pmin = None
    if txt != "-":
        if not txt.isdigit(): await message.answer("Введите число или «-»."); return
        pmin = int(txt)
    await state.update_data(price_min=pmin); await state.set_state(SearchWizard.price_max)
    await message.answer("Максимальная цена (или «-»):", reply_markup=types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="-")],[types.KeyboardButton(text="Отмена")]], resize_keyboard=True))

@wizard_router.message(SearchWizard.price_max, F.text.casefold() == "отмена")
async def wizard_cancel_max(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.price_max)
async def wizard_got_max(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip(); pmax = None
    if txt != "-":
        if not txt.isdigit(): await message.answer("Введите число или «-»."); return
        pmax = int(txt)
    await state.update_data(price_max=pmax); await state.set_state(SearchWizard.sort_date)
    kb = types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="Да, сортировка по дате")],
                                             [types.KeyboardButton(text="Нет, оставить как есть")],
                                             [types.KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer("Добавить к ссылке сортировку «по дате»?", reply_markup=kb)

@wizard_router.message(SearchWizard.sort_date, F.text.casefold() == "отмена")
async def wizard_cancel_sort(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Мастер отменён.", reply_markup=MAIN_KB)

@wizard_router.message(SearchWizard.sort_date)
async def wizard_got_sort(message: types.Message, state: FSMContext):
    sort = "date" if "да" in (message.text or "").lower() else "none"
    await state.update_data(sort=sort)
    data = await state.get_data()
    region = data.get("region") or "rossiya"; category = data.get("category") or ""
    kws = data.get("keywords", []); pmin = data.get("price_min"); pmax = data.get("price_max"); sort = data.get("sort", "none")
    from urllib.parse import urlencode
    query = {}; 
    if kws: query["q"] = " ".join(kws)
    if sort == "date": query["s"] = "104"
    url = f"https://www.avito.ru/{region}" + (f"/{category}" if category and category != "-" else "")
    url += ("?" + urlencode(query)) if query else ""
    parts = [f"<b>Регион:</b> {region}"]
    if category and category != "-": parts.append(f"<b>Категория:</b> {category}")
    if kws: parts.append(f"<b>Ключевые слова:</b> {', '.join(kws)}")
    if pmin is not None: parts.append(f"<b>Мин. цена:</b> {pmin}")
    if pmax is not None: parts.append(f"<b>Макс. цена:</b> {pmax}")
    if sort == "date": parts.append("<b>Сортировка:</b> по дате")
    parts.append(f"<b>URL:</b> {url}")
    await state.set_state(SearchWizard.confirm)
    await message.answer("\n".join(parts), reply_markup=types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="Создать"), types.KeyboardButton(text="Отмена")]], resize_keyboard=True), disable_web_page_preview=True)

class SupportDialog(StatesGroup):
    message = State()

support_router = Router(name="support")

@support_router.message(F.text.in_(["🛟 Поддержка", "Поддержка"]))
async def support_start(message: types.Message, state: FSMContext):
    if not ADMIN_CHAT_ID:
        await message.answer("Поддержка не настроена. Администратор не указал ADMIN_CHAT_ID в .env"); return
    await state.set_state(SupportDialog.message)
    await message.answer("Опишите проблему одним или несколькими сообщениями. Я всё перешлю администратору.\nНапишите «Отмена» чтобы выйти.")

@support_router.message(SupportDialog.message, F.text.casefold() == "отмена")
async def support_cancel(message: types.Message, state: FSMContext):
    await state.clear(); await message.answer("Диалог с поддержкой завершён.", reply_markup=MAIN_KB)

@support_router.message(SupportDialog.message)
async def support_relay(message: types.Message, state: FSMContext):
    try:
        username = ("@"+message.from_user.username) if message.from_user and message.from_user.username else "username отсутствует"
        fn = (message.from_user.first_name or "").strip() if message.from_user else ""
        ln = (message.from_user.last_name or "").strip() if message.from_user else ""
        full_name = (fn + (" " + ln if ln else "")).strip() or "Без имени"
        safe_header = "📩 <b>Новое сообщение в поддержку</b>\nОт: " + html.escape(full_name) + " (" + html.escape(username) + ")"
        await message.bot.send_message(int(ADMIN_CHAT_ID), safe_header, disable_web_page_preview=True)
        await message.bot.copy_message(int(ADMIN_CHAT_ID), from_chat_id=message.chat.id, message_id=message.message_id)
        await message.answer("✅ Отправлено в поддержку. Мы свяжемся с вами в личке, как только ответим.", reply_markup=MAIN_KB)
        await state.clear()
    except Exception:
        await message.answer("Не удалось отправить сообщение в поддержку. Попробуйте позже.", reply_markup=MAIN_KB)
        await state.clear()

class App:
    def __init__(self, token: str):
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher(storage=MemoryStorage())
        self.manager = WatcherManager(self.bot)
        self._register()

    async def setup_menu(self):
        await self.bot.set_my_commands([
            types.BotCommand(command="start", description="Начать работу"),
            types.BotCommand(command="add", description="Добавить ссылку для мониторинга"),
            types.BotCommand(command="list", description="Список ваших подписок"),
            types.BotCommand(command="remove", description="Удалить подписку по ID"),
            types.BotCommand(command="feed", description="Лента присланных объявлений"),
            types.BotCommand(command="newsearch", description="Мастер подбора фильтров"),
        ])

    def _register(self):
        self.dp.include_router(wizard_router); self.dp.include_router(support_router)

        @self.dp.message(Command("start"))
        async def start_cmd(m: types.Message):
            await m.answer("Я пришлю вам <b>только что опубликованные</b> объявления по вашим фильтрам.\n\n• Быстрый старт: <code>/add https://www.avito.ru/...</code>\n• Мастер: <code>/newsearch</code> или кнопка «🧭 Поиски»\n• Смотреть, что пришло: <code>/feed</code> или кнопка «🗂 Лента»", reply_markup=MAIN_KB, disable_web_page_preview=True)

        @self.dp.message(Command("add"))
        async def add_cmd(m: types.Message):
            txt = (m.text or ""); parts = txt.split(maxsplit=2)
            if len(parts) < 2:
                await m.reply("Укажите ссылку после /add. Пример:\n/add https://www.avito.ru/... kw=iphone,min=10000"); return
            url = parts[1].strip(); params = parts[2].strip() if len(parts) >= 3 else ""
            flt = SubscriberFilter()
            if params:
                p = params.replace(" ", "")
                for token in p.split(","):
                    if not token: continue
                    if token.startswith("kw="):
                        kws = token[3:].replace(";", ",").replace("|", ",")
                        flt.keywords_all = [w for w in kws.split(",") if w]
                    elif token.startswith("min="):
                        try: flt.price_min = int(token[4:])
                        except: pass
                    elif token.startswith("max="):
                        try: flt.price_max = int(token[4:])
                        except: pass
            sub = await self.manager.add_subscription(m.chat.id, url, flt)
            await m.reply(f"Подписка добавлена: <b>{sub.id}</b>\nСсылка: {sub.url}\nПодсказка: нажмите «🗂 Лента», чтобы посмотреть, что уже пришло по вашим фильтрам.")

        @self.dp.message(Command("list"))
        async def list_cmd(m: types.Message):
            subs = self.manager.list_user_subs(m.chat.id)
            if not subs: await m.reply("У вас нет активных подписок."); return
            lines = ["Ваши подписки:"]
            for s in subs:
                desc = []
                if s.flt.keywords_all: desc.append(f"kw={','.join(s.flt.keywords_all)}")
                if s.flt.price_min is not None: desc.append(f"min={s.flt.price_min}")
                if s.flt.price_max is not None: desc.append(f"max={s.flt.price_max}")
                lines.append(f"{s.id}: {s.url} " + (f"({'; '.join(desc)})" if desc else ""))
            lines.append("\nПодсказка: /feed — последние присланные карточки.")
            await m.reply("\n".join(lines), disable_web_page_preview=True)

        @self.dp.message(Command("remove"))
        async def remove_cmd(m: types.Message):
            parts = (m.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                await m.reply("Укажите ID: /remove 3"); return
            ok = await self.manager.remove_subscription(m.chat.id, int(parts[1].strip()))
            await m.reply("Удалено." if ok else "Подписка не найдена.")

        @self.dp.message(Command("feed"))
        async def feed_cmd(m: types.Message):
            items = self.manager.recent_feed(m.chat.id, limit=10)
            if not items:
                await m.reply("Пока в ленте пусто. Как только появятся новые — они будут здесь."); return
            lines = ["🗂 Последние присланные объявления:"]
            for it in items:
                price_txt = f"{it.price} ₽" if it.price is not None else "—"
                dt = datetime.fromtimestamp(it.ts).strftime("%H:%M:%S")
                lines.append(f"• {it.title} — {price_txt} — {dt}\n{it.url}")
            await m.reply("\n".join(lines), disable_web_page_preview=True)

        @self.dp.message(F.text.in_(["🗂 Лента", "Лента"]))
        async def feed_btn(m: types.Message):
            items = self.manager.recent_feed(m.chat.id, limit=10)
            if not items:
                await m.reply("Пока в ленте пусто. Как только появятся новые — они будут здесь."); return
            lines = ["🗂 Последние присланные объявления:"]
            for it in items:
                price_txt = f"{it.price} ₽" if it.price is not None else "—"
                dt = datetime.fromtimestamp(it.ts).strftime("%H:%M:%S")
                lines.append(f"• {it.title} — {price_txt} — {dt}\n{it.url}")
            await m.reply("\n".join(lines), disable_web_page_preview=True)

        @self.dp.message(SearchWizard.confirm, F.text.casefold() == "создать")
        async def wizard_confirm(m: types.Message, state: FSMContext):
            data = await state.get_data()
            region = data.get("region") or "rossiya"; category = data.get("category") or ""
            kws = data.get("keywords", []); pmin = data.get("price_min"); pmax = data.get("price_max"); sort = data.get("sort", "none")
            from urllib.parse import urlencode
            query = {}
            if kws: query["q"] = " ".join(kws)
            if sort == "date": query["s"] = "104"
            url = f"https://www.avito.ru/{region}" + (f"/{category}" if category and category != "-" else "")
            url += ("?" + urlencode(query)) if query else ""
            flt = SubscriberFilter(keywords_all=kws, price_min=pmin, price_max=pmax)
            sub = await self.manager.add_subscription(m.chat.id, url, flt)
            await state.clear()
            await m.answer(f"Подписка создана: <b>{sub.id}</b>\nURL: {url}\nФильтры: " + (f"kw={','.join(kws)} " if kws else "") + (f"min={pmin} " if pmin is not None else "") + (f"max={pmax} " if pmax is not None else "") + (" сортировка=по дате" if sort == "date" else "") + "\n\nНажмите «🗂 Лента», чтобы смотреть последние присланные карточки.", reply_markup=MAIN_KB, disable_web_page_preview=True)

async def main():
    load_dotenv()  # это уже есть в файле
    keep_alive()   # добавь эту строку
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN отсутствует в .env")
    app = App(token)
    await app.setup_menu()
    await app.dp.start_polling(app.bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
