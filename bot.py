"""
Monitor de vendinha Herosaga.

Uso:
    python bot.py

Variáveis de ambiente:
- TOKEN: token do bot Telegram
- CHAT_ID: chat id para enviar notificações
- SHOP_URL: url da lojinha
- DISCORD_WEBHOOK: webhook opcional para futuras notificações no Discord
- DISCORD_MESSAGE: texto personalizado opcional para mensagens no Discord
- NOTIFY_COOLDOWN: tempo mínimo entre alertas do mesmo item, em segundos
- REQUEST_TIMEOUT: timeout das requisições HTTP, em segundos

O script faz scraping da página da vendinha, salva histórico em data/history.json,
detecta quedas de quantidade (venda) e notifica via Telegram/Discord.
Ele executa um único ciclo por processo, o que o torna compatível com GitHub Actions.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
SHOP_URLS_FILE = CONFIG_DIR / "shop_urls.txt"
DATA_DIR = ROOT_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    current_key: str | None = None
    current_value_parts: list[str] = []

    def flush_current() -> None:
        nonlocal current_key, current_value_parts
        if current_key is not None:
            values[current_key] = "\n".join(current_value_parts).rstrip("\n")
        current_key = None
        current_value_parts = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        if stripped.startswith("#"):
            flush_current()
            continue

        if not stripped:
            if current_key is not None:
                current_value_parts.append("")
            continue

        if "=" in line and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            flush_current()
            key, value = line.split("=", 1)
            current_key = key.strip()
            current_value_parts = [value]
            continue

        if current_key is not None:
            current_value_parts.append(line)

    flush_current()
    return values


def load_env_overrides(path: Path) -> None:
    env_values = load_env_file(path)
    for key, value in env_values.items():
        os.environ.setdefault(key, value)


load_env_overrides(ROOT_DIR / ".env")

DEFAULT_SHOP_URL = "https://herosaga.com.br/?module=vending&action=viewshop&id=30313"
# Prefere as variáveis usadas no .env local, sem perder compatibilidade com o GitHub Actions.
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
# `CHAT_IDS` prepara o envio para múltiplos usuários sem quebrar o fluxo atual.
CHAT_IDS = [chat.strip() for chat in os.getenv("CHAT_IDS", "").split(",") if chat.strip()]
if not CHAT_IDS and CHAT_ID:
    CHAT_IDS = [CHAT_ID]
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_MESSAGE = os.getenv("DISCORD_MESSAGE", "").strip()
TELEGRAM_MESSAGE = os.getenv("TELEGRAM_MESSAGE", "").strip()
SHOP_URL = os.getenv("SHOP_URL", DEFAULT_SHOP_URL)
STORE_TARGETS = []
BASE_URL = "https://herosaga.com.br/"
TELEGRAM_DISABLE_NOTIFICATION = os.getenv("TELEGRAM_DISABLE_NOTIFICATION", "false").strip().casefold() in {"1", "true", "yes", "on"}
try:
    LOCAL_TZ = ZoneInfo("America/Belem")
except Exception:
    try:
        LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
    except Exception:
        LOCAL_TZ = timezone(timedelta(hours=-3))

CURRENCY_KEYWORDS = {
    "Hero Points": [
        "hero point",
        "hero points",
        "h point",
        "h points",
    ],
    "Moedas RMT": [
        "moeda rmt",
        "moedas rmt",
        "rmt",
    ],
}


def detect_currency_from_text(text: str) -> str | None:
    normalized = (text or "").casefold()
    if not normalized:
        return None

    for label, keywords in CURRENCY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized:
                return label
    return None


def detect_currency_for_sale(sold_item_name: str, inventory: dict[str, "ShopItem"]) -> str:
    sold_currency = detect_currency_from_text(sold_item_name)
    if sold_currency:
        return sold_currency

    counts = {"Hero Points": 0, "Moedas RMT": 0}
    for item in inventory.values():
        currency = detect_currency_from_text(item.name)
        if currency in counts:
            counts[currency] += 1

    if counts["Hero Points"] > counts["Moedas RMT"]:
        return "Hero Points"
    if counts["Moedas RMT"] > counts["Hero Points"]:
        return "Moedas RMT"
    return "Não identificado"


def normalize_sale_currency_label(currency: str) -> str:
    normalized = (currency or "").strip().casefold()
    if normalized in {"hero points", "hero point", "h point", "h points"}:
        return "Hero Points"
    if normalized in {"moedas rmt", "moeda rmt", "rmt"}:
        return "Moeda RMT"
    if normalized in {"zeny", "c", "coins", "coin"}:
        return "Zeny"
    return currency or "Não identificado"


def detect_currency_for_price(price: str | None) -> str:
    if not price:
        return "Não identificado"

    normalized_price = price.casefold()
    if re.search(r"\b(?:hero\s*points?|h\s*points?|hp)\b", normalized_price):
        return "Hero Points"
    if re.search(r"\b(?:rmt|moeda[s]?\s*rmt)\b", normalized_price):
        return "Moeda RMT"
    if re.search(r"\b(?:zeny|z)\b", normalized_price):
        return "Zeny"
    return "Não identificado"


def format_sale_price(price: str | None, currency: str) -> str:
    price_text = (price or "-").strip()
    currency_label = normalize_sale_currency_label(currency)

    if not price_text or price_text == "-":
        return "-"

    if currency_label == "Zeny":
        return re.sub(r"\s*(?:c|zeny|z)\s*$", "", price_text, flags=re.I).strip() or price_text

    if currency_label == "Hero Points":
        return re.sub(r"\s*(?:c|hero points?|h points?|hp)\s*$", "", price_text, flags=re.I).strip() or price_text

    if currency_label == "Moeda RMT":
        return re.sub(r"\s*(?:moeda[s]?\s*rmt|rmt)\s*$", "", price_text, flags=re.I).strip() or price_text

    return price_text


@dataclass(frozen=True)
class StoreTarget:
    name: str
    url: str
    currency: str = ""


def parse_store_target(line: str, index: int) -> StoreTarget | None:
    raw = line.strip()
    if not raw:
        return None

    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        name = parts[0] or f"loja-{index}"
        url = parts[1] if len(parts) > 1 else ""
        currency = parts[2] if len(parts) > 2 else ""
    else:
        name = f"loja-{index}"
        url = raw
        currency = ""

    if not url:
        return None

    return StoreTarget(name=name, url=url, currency=currency)


def load_store_targets() -> list[StoreTarget]:
    if SHOP_URLS_FILE.exists():
        targets = []
        for line in SHOP_URLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            target = parse_store_target(line, len(targets) + 1)
            if target:
                targets.append(target)
        if targets:
            return targets

    env_value = os.getenv("SHOP_URLS", "").strip()
    if env_value:
        targets = []
        raw_items = []
        for line in env_value.splitlines():
            raw_items.extend([part.strip() for part in line.split(",") if part.strip()])

        for raw in raw_items:
            target = parse_store_target(raw, len(targets) + 1)
            if target:
                targets.append(target)

        if targets:
            return targets

    fallback = (os.getenv("SHOP_URL", DEFAULT_SHOP_URL) or DEFAULT_SHOP_URL).strip()
    return [StoreTarget(name="loja-1", url=fallback)]


def resolve_currency(target: StoreTarget, item_name: str, inventory: dict[str, "ShopItem"], price: str | None = None) -> str:
    configured_currency = normalize_sale_currency_label(target.currency)
    if configured_currency != "Não identificado":
        return configured_currency

    detected_currency = detect_currency_for_sale(item_name, inventory)
    if detected_currency != "Não identificado":
        return normalize_sale_currency_label(detected_currency)

    price_currency = detect_currency_for_price(price)
    if price_currency != "Não identificado":
        return normalize_sale_currency_label(price_currency)

    return "Hero Points"


STORE_TARGETS = load_store_targets()

COOLDOWN = int(os.getenv("NOTIFY_COOLDOWN", "30"))  # segundos entre notificações por item
STORE_ISSUE_COOLDOWN = int(os.getenv("STORE_ISSUE_COOLDOWN", "86400"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
STATE_VERSION = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vend-monitor")


def build_message_prefix(target: StoreTarget) -> str:
    return f"Loja: {target.name}\nURL: {target.url}\n"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_history():
    ensure_data_dir()
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if history.get("version") == STATE_VERSION:
                return history

            if isinstance(history, dict) and ("stores" in history or "quantities" in history or "notified" in history):
                migrated = {"version": STATE_VERSION, "stores": {}, "alerts": {}}

                if isinstance(history.get("stores"), dict):
                    migrated["stores"] = history.get("stores", {})
                elif history.get("quantities") is not None or history.get("notified") is not None:
                    legacy_store_key = STORE_TARGETS[0].url if STORE_TARGETS else SHOP_URL
                    migrated["stores"][legacy_store_key] = {
                        "quantities": history.get("quantities", {}),
                        "last_alerts": history.get("notified", {}),
                        "last_issue_alerts": {},
                    }

                if isinstance(history.get("alerts"), dict):
                    migrated["alerts"] = history.get("alerts", {})

                logger.info("Histórico migrado para o formato atual")
                save_history(migrated)
                return migrated

            return history
        except Exception:
            logger.warning("Histórico corrompido ou inválido; reiniciando estado local")
            return {"version": STATE_VERSION, "stores": {}, "alerts": {}}
    return {"version": STATE_VERSION, "stores": {}, "alerts": {}}


def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_shop(html: str) -> dict:
    """Tenta extrair pares nome->quantidade da página da vendinha.
    Usa heurísticas: varre <tr>, linhas com números, e busca por padrões comuns.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = {}

    # Heurística 1: procurar linhas de tabela
    for tr in soup.find_all("tr"):
        texts = [t.strip() for t in tr.stripped_strings]
        if not texts:
            continue
        # coletar tokens que contenham números
        num_tokens = [s for s in texts if re.search(r"\d+", s)]
        if not num_tokens:
            continue
        # quantity: usar último número encontrado na linha
        qty_match = re.search(r"(\d+)(?!.*\d)", " ".join(texts))
        if not qty_match:
            continue
        qty = int(qty_match.group(1))
        # name: juntar tokens sem o número encontrado
        joined = " ".join(texts)
        name = re.sub(r"\b" + re.escape(qty_match.group(1)) + r"\b", "", joined)
        name = re.sub(r"\s{2,}", " ", name).strip()
        if name and len(name) < 200:
            items[name] = qty

    # Heurística 2: procurar blocos com classes que contenham 'item' ou 'vending'
    if not items:
        for div in soup.find_all(class_=re.compile(r"item|vending|produto|shop", re.I)):
            texts = [t.strip() for t in div.stripped_strings]
            if not texts:
                continue
            qty_match = None
            for s in texts:
                m = re.search(r"\b(\d+)\b", s)
                if m:
                    qty_match = m
            if qty_match:
                qty = int(qty_match.group(1))
                name = " ".join([t for t in texts if qty_match.group(1) not in t])
                name = re.sub(r"\s{2,}", " ", name).strip()
                if name:
                    items[name] = qty

    # Heurística final: tentar mapear por linhas de texto (fallback)
    if not items:
        text = soup.get_text("\n")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r"(.+?)\b(\d+)\b$", line)
            if m:
                name = m.group(1).strip()
                qty = int(m.group(2))
                items[name] = qty

    return items


@dataclass(frozen=True)
class ShopItem:
    item_id: str
    name: str
    quantity: int
    price: str | None = None
    image_url: str | None = None


def parse_shop_inventory(html: str) -> dict[str, ShopItem]:
    soup = BeautifulSoup(html, "html.parser")
    inventory: dict[str, ShopItem] = {}

    rows = soup.select("table.items-table tbody tr") or soup.find_all("tr")

    for index, tr in enumerate(rows, start=1):
        texts = [t.strip() for t in tr.stripped_strings if t.strip()]
        if len(texts) < 2:
            continue

        item_id = None
        item_id_cell = tr.select_one("td.item-id a")
        if item_id_cell:
            item_id_match = re.search(r"(\d+)", item_id_cell.get_text(strip=True))
            if item_id_match:
                item_id = item_id_match.group(1)

        if item_id is None:
            numeric_tokens = [token for token in texts if re.fullmatch(r"\d+", token)]
            if numeric_tokens:
                item_id = numeric_tokens[0]

        if item_id is None:
            item_id = f"row-{index}"

        name_cell = tr.select_one(".item-name-cell a") or tr.select_one("td:nth-of-type(2)")
        item_name = name_cell.get_text(" ", strip=True) if name_cell else texts[1].strip()
        if not item_name:
            continue

        quantity_cell = tr.select_one(".item-amount") or tr.select_one("td:last-child")
        qty_source = quantity_cell.get_text(" ", strip=True) if quantity_cell else " ".join(texts)
        qty_match = re.search(r"(\d+)(?!.*\d)", qty_source)
        if not qty_match:
            continue

        quantity = int(qty_match.group(1))
        price_cell = tr.select_one(".item-price") or tr.select_one("td:nth-last-child(2)")
        price = price_cell.get_text(" ", strip=True) if price_cell else (texts[-2].strip() if len(texts) >= 2 else None)
        if price and re.fullmatch(r"\d+", price):
            price = None

        image_url = None
        img = tr.find("img")
        if img and img.get("src"):
            image_url = urljoin(BASE_URL, img.get("src"))

        inventory[item_id] = ShopItem(
            item_id=item_id,
            name=item_name,
            quantity=quantity,
            price=price,
            image_url=image_url,
        )

    if inventory:
        return inventory

    for index, div in enumerate(soup.select(".item, .vending-item, .produto, .shop-item"), start=1):
        texts = [t.strip() for t in div.stripped_strings if t.strip()]
        if len(texts) < 2:
            continue

        qty_match = re.search(r"(\d+)(?!.*\d)", " ".join(texts))
        if not qty_match:
            continue

        item_id_match = re.search(r"\b(\d{4,})\b", " ".join(texts))
        item_id = item_id_match.group(1) if item_id_match else f"row-{index}"
        item_name = texts[1].strip()
        if not item_name:
            continue

        quantity = int(qty_match.group(1))
        price_match = re.search(r"(\d[\d.,]*\s*(?:c|z|zeny|hero points?|moedas?|rmt))", " ".join(texts), re.I)
        price = price_match.group(1).strip() if price_match else None
        image_url = None
        img = div.find("img")
        if img and img.get("src"):
            image_url = urljoin(BASE_URL, img.get("src"))

        inventory[item_id] = ShopItem(
            item_id=item_id,
            name=item_name,
            quantity=quantity,
            price=price,
            image_url=image_url,
        )

    return inventory


def fetch_shop(url: str) -> str:
    headers = {"User-Agent": "vend-monitor/1.0 (+https://github.com)"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def shop_is_unavailable(html: str) -> bool:
    normalized = (html or "").casefold()
    return (
        "vendedor não encontrado" in normalized
        or "vendedor nao encontrado" in normalized
        or "seller not found" in normalized
    )


def telegram_api_request(method: str, payload: dict | None = None) -> requests.Response:
    if not TOKEN:
        raise RuntimeError("TOKEN ausente")

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    return requests.post(url, json=payload or {}, timeout=10)


def send_telegram(text: str, chat_ids: list[str] | None = None, image_url: str | None = None) -> bool:
    recipients = chat_ids or CHAT_IDS
    if not TOKEN or not recipients:
        logger.warning("Telegram não configurado; pulando envio")
        return False

    normalized_text = text.casefold()
    if "teste de conectividade" in normalized_text:
        logger.warning("Bloqueando mensagem de teste de conectividade no Telegram")
        return False

    sent_any = False
    try:
        for chat_id in recipients:
            if image_url:
                payload = {
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": text[:1024],
                    "disable_notification": TELEGRAM_DISABLE_NOTIFICATION,
                }
                r = telegram_api_request("sendPhoto", payload)
                if not r.ok:
                    logger.warning("Telegram rejeitou a foto; reenviando como texto")
                    payload = {"chat_id": chat_id, "text": text, "disable_notification": TELEGRAM_DISABLE_NOTIFICATION}
                    r = telegram_api_request("sendMessage", payload)
            else:
                payload = {"chat_id": chat_id, "text": text, "disable_notification": TELEGRAM_DISABLE_NOTIFICATION}
                r = telegram_api_request("sendMessage", payload)
            if r.ok:
                logger.info("Alerta enviado")
                sent_any = True
            else:
                logger.error("Erro Telegram: %s", r.text)
        return sent_any
    except Exception as e:
        logger.exception("Erro Telegram: %s", e)
        return False


def build_discord_message(
    default_text: str,
    target: StoreTarget,
    name: str,
    quantity: int,
    bought_quantity: int,
    price: str | None,
    now: str,
    currency: str,
    image_url: str | None = None,
    hero_points: str | None = None,
) -> str:
    if not DISCORD_MESSAGE:
        return default_text

    resolved_hero_points = hero_points or (price or "-")

    replacements = {
        "{default}": default_text,
        "{text}": default_text,
        "{store}": target.name,
        "{shop}": target.name,
        "{currency}": currency,
        "{coin}": currency,
        "{item}": name,
        "{quantity}": str(quantity),
        "{bought_quantity}": str(bought_quantity),
        "{price}": price or "-",
        "{time}": now,
        "{hero_points}": resolved_hero_points,
        "{img_url}": image_url or "",
    }

    message = DISCORD_MESSAGE
    for key, value in replacements.items():
        message = message.replace(key, value)
    return message or default_text


def build_telegram_message(
    default_text: str,
    target: StoreTarget,
    name: str,
    quantity: int,
    bought_quantity: int,
    price: str | None,
    now: str,
    currency: str,
    image_url: str | None = None,
    hero_points: str | None = None,
) -> str:
    if not TELEGRAM_MESSAGE:
        return default_text

    resolved_hero_points = hero_points or (price or "-")

    replacements = {
        "{default}": default_text,
        "{text}": default_text,
        "{store}": target.name,
        "{shop}": target.name,
        "{currency}": currency,
        "{coin}": currency,
        "{item}": name,
        "{quantity}": str(quantity),
        "{bought_quantity}": str(bought_quantity),
        "{price}": price or "-",
        "{time}": now,
        "{hero_points}": resolved_hero_points,
        "{img_url}": image_url or "",
    }

    message = TELEGRAM_MESSAGE
    for key, value in replacements.items():
        message = message.replace(key, value)
    return message or default_text


def send_discord(text: str, image_url: str | None = None) -> bool:
    if not DISCORD_WEBHOOK:
        return False
    try:
        payload: dict = {"content": text}
        if image_url:
            payload["embeds"] = [{"image": {"url": image_url}}]
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.ok:
            logger.info("Alerta enviado via Discord")
            return True
        else:
            logger.warning("Falha ao enviar Discord: %s", r.text)
            return False
    except Exception:
        logger.exception("Erro enviando Discord")
        return False


def now_sao_paulo() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_sale_message(
    target: StoreTarget,
    name: str,
    before: int,
    after: int,
    price: str | None = None,
    currency: str = "Não identificado",
    now: datetime | None = None,
) -> str:
    current_time = (now or now_sao_paulo()).strftime("%d/%m/%Y %H:%M:%S")
    bought_quantity = max(before - after, 0)
    price_text = format_sale_price(price, currency)
    currency_text = normalize_sale_currency_label(currency)
    return (
        f"🛒 VENDA DETECTADA\n"
        f"Loja: {target.name}\n"
        f"Moeda: {currency_text}\n"
        f"Item: {name}\n"
        f"Quantidade: {after}\n"
        f"Quantidade comprada: {bought_quantity}\n"
        f"Preço: {price_text}\n"
        f"Hora: {current_time}"
    )


def format_hero_points(price: str | None, fallback: int | str) -> str:
    if price:
        normalized = re.sub(r"\s*(?:c|rmt|hero points?|moedas?)\s*$", "", price, flags=re.I).strip()
        if normalized:
            return normalized
    return str(fallback)


def notify_sale(
    target: StoreTarget,
    name: str,
    before: int,
    after: int,
    price: str | None = None,
    image_url: str | None = None,
    currency: str = "Não identificado",
) -> bool:
    current_time = now_sao_paulo()
    bought_quantity = max(before - after, 0)
    resolved_currency = normalize_sale_currency_label(currency)
    if resolved_currency == "Não identificado":
        resolved_currency = normalize_sale_currency_label(detect_currency_for_price(price))
    default_text = format_sale_message(target, name, before, after, price, resolved_currency, now=current_time)
    now = current_time.strftime("%d/%m/%Y %H:%M:%S")
    hero_points = format_hero_points(price, after)
    telegram_text = build_telegram_message(
        default_text,
        target,
        name,
        after,
        bought_quantity,
        price,
        now,
        resolved_currency,
        image_url=image_url,
        hero_points=hero_points,
    )
    discord_text = build_discord_message(
        default_text,
        target,
        name,
        after,
        bought_quantity,
        price,
        now,
        resolved_currency,
        image_url=image_url,
        hero_points=hero_points,
    )
    telegram_sent = send_telegram(telegram_text, image_url=image_url)
    discord_sent = send_discord(discord_text, image_url=image_url)
    if telegram_sent and discord_sent:
        logger.info("Alerta enviado para %s (Telegram e Discord)", name)
        return True
    if telegram_sent:
        logger.info("Alerta enviado para %s (Telegram)", name)
        return True
    if discord_sent:
        logger.warning("Discord enviou o alerta de %s, mas Telegram falhou; verifique TOKEN/CHAT_ID", name)
        return True
    logger.warning("Nenhum canal conseguiu enviar o alerta de %s", name)
    return False


def notify_store_issue(target: StoreTarget) -> bool:
    current_time = now_sao_paulo().strftime("%d/%m/%Y %H:%M:%S")
    default_text = (
        "⚠️ IMPORTANTE\n"
        f"Loja: {target.name}\n"
        f"URL: {target.url}\n"
        "Status: vendedor não encontrado ou loja indisponível\n"
        "Possível mudança de ID após manutenção\n"
        "Ação sugerida: conferir o novo ID da loja e atualizar config/shop_urls.txt\n"
        f"Hora: {current_time}"
    )

    telegram_text = build_telegram_message(
        default_text,
        target,
        "Loja indisponível",
        0,
        0,
        price=None,
        now=current_time,
        currency="Não identificado",
        image_url=None,
        hero_points=None,
    )
    discord_text = build_discord_message(
        default_text,
        target,
        "Loja indisponível",
        0,
        0,
        price=None,
        now=current_time,
        currency="Não identificado",
        image_url=None,
        hero_points=None,
    )

    telegram_sent = send_telegram(telegram_text)
    discord_sent = send_discord(discord_text)
    if telegram_sent and discord_sent:
        logger.info("Aviso importante enviado para %s (Telegram e Discord)", target.name)
        return True
    if telegram_sent:
        logger.info("Aviso importante enviado para %s (Telegram)", target.name)
        return True
    if discord_sent:
        logger.warning("Discord enviou o aviso importante de %s, mas Telegram falhou", target.name)
        return True

    logger.warning("Nenhum canal conseguiu enviar o aviso importante de %s", target.name)
    return False


def run_smoke_test() -> int:
    if not TOKEN or not CHAT_IDS:
        logger.error("TOKEN e CHAT_ID são obrigatórios para enviar o teste")
        return 1

    target = StoreTarget(name="Teste do app", url="https://herosaga.com.br/")
    now = now_sao_paulo().strftime("%d/%m/%Y %H:%M:%S")
    default_text = format_sale_message(
        target,
        "Mensagem de teste",
        2,
        1,
        price="18,000",
        currency="Hero Points",
    )
    telegram_text = build_telegram_message(
        default_text,
        target,
        "Mensagem de teste",
        1,
        1,
        "18,000",
        now,
        "Hero Points",
        image_url=None,
        hero_points="18,000",
    )
    discord_text = build_discord_message(
        default_text,
        target,
        "Mensagem de teste",
        1,
        1,
        "18,000",
        now,
        "Hero Points",
        image_url=None,
        hero_points="18,000",
    )

    telegram_sent = send_telegram(telegram_text)
    discord_sent = send_discord(discord_text)
    if telegram_sent or discord_sent:
        logger.info("Teste manual enviado com sucesso")
        return 0

    logger.error("Teste manual não enviou nenhuma notificação")
    return 1


def load_store_state(history: dict, target: StoreTarget) -> dict:
    stores = history.setdefault("stores", {})
    return stores.setdefault(
        target.url,
        {"quantities": {}, "last_alerts": {}, "items_meta": {}, "last_issue_alerts": {}},
    )


def should_alert(last_alerts: dict, key: str, now_ts: int, cooldown: int = COOLDOWN) -> bool:
    last_seen = int(last_alerts.get(key, 0))
    return now_ts - last_seen >= cooldown


def register_alert(last_alerts: dict, key: str, now_ts: int):
    last_alerts[key] = now_ts


def inspect_store(target: StoreTarget, history: dict) -> dict:
    logger.info("Carregando lojas: %s", target.name)
    store_state = load_store_state(history, target)
    quantities = store_state.setdefault("quantities", {})
    last_alerts = store_state.setdefault("last_alerts", {})
    items_meta = store_state.setdefault("items_meta", {})
    last_issue_alerts = store_state.setdefault("last_issue_alerts", {})

    logger.info("Verificando itens: %s", target.url)
    now_ts = int(time.time())
    html = fetch_shop(target.url)
    if shop_is_unavailable(html):
        logger.warning("Loja indisponível ou ID antigo: %s", target.url)
        issue_key = f"{target.url}:unavailable"
        if should_alert(last_issue_alerts, issue_key, now_ts, STORE_ISSUE_COOLDOWN):
            if notify_store_issue(target):
                register_alert(last_issue_alerts, issue_key, now_ts)
        return {"items_found": 0, "changes": [], "unavailable": True}

    inventory = parse_shop_inventory(html)
    items = {item_key: item.quantity for item_key, item in inventory.items()}
    changes = []

    if not items:
        logger.warning("Nenhum item detectado na página — verifique o parser ou a URL.")
        return {"items_found": 0, "changes": changes}

    logger.info("Itens encontrados na loja %s: %d", target.name, len(items))

    for item_key, qty in items.items():
        item = inventory.get(item_key)
        item_name = item.name if item else item_key
        logger.info("Item encontrado: %s | quantidade=%s", item_name, qty)
        currency = resolve_currency(target, item_name, inventory, price=item.price if item else None)
        if currency == "Não identificado":
            cached_currency = items_meta.get(item_key, {}).get("currency")
            if cached_currency:
                currency = normalize_sale_currency_label(cached_currency)

        items_meta[item_key] = {
            "price": item.price if item else None,
            "image_url": item.image_url if item else None,
            "currency": currency,
        }

        prev = quantities.get(item_key)
        if prev is None:
            quantities[item_key] = qty
            continue

        if qty < prev:
            alert_key = f"{target.url}:{item_key}"
            if should_alert(last_alerts, alert_key, now_ts):
                sale_meta = items_meta.get(item_key, {})
                sale_currency = resolve_currency(target, item_name, inventory, price=(item.price if item else None) or sale_meta.get("price"))
                if sale_currency == "Não identificado":
                    sale_currency = normalize_sale_currency_label(sale_meta.get("currency", "Não identificado"))
                if notify_sale(
                    target,
                    item_name,
                    prev,
                    qty,
                    price=(item.price if item else None) or sale_meta.get("price"),
                    image_url=(item.image_url if item else None) or sale_meta.get("image_url"),
                    currency=sale_currency,
                ):
                    register_alert(last_alerts, alert_key, now_ts)
                    changes.append((item_name, prev, qty))
            else:
                logger.warning("Mudança repetida em cooldown para %s", item_name)

        quantities[item_key] = qty

    missing_item_keys = [item_key for item_key in quantities.keys() if item_key not in items]
    for item_key in missing_item_keys:
        prev = quantities.get(item_key)
        if prev is None:
            continue
        if prev == 0:
            continue

        item = inventory.get(item_key)
        item_name = item.name if item else item_key
        sale_meta = items_meta.get(item_key, {})
        alert_key = f"{target.url}:{item_key}"
        if should_alert(last_alerts, alert_key, now_ts):
            sale_currency = resolve_currency(target, item_name, inventory, price=(item.price if item else None) or sale_meta.get("price"))
            if sale_currency == "Não identificado":
                sale_currency = normalize_sale_currency_label(sale_meta.get("currency", "Não identificado"))
            if notify_sale(
                target,
                item_name,
                prev,
                0,
                price=(item.price if item else None) or sale_meta.get("price"),
                image_url=(item.image_url if item else None) or sale_meta.get("image_url"),
                currency=sale_currency,
            ):
                register_alert(last_alerts, alert_key, now_ts)
                changes.append((item_name, prev, 0))
        else:
            logger.warning("Mudança repetida em cooldown para %s", item_name)

        quantities[item_key] = 0

    if not changes:
        logger.info("Nenhuma mudança encontrada em %s", target.name)

    return {"items_found": len(items), "changes": changes}

def check_once(history: dict) -> dict:
    results = []
    total_items = 0
    total_changes = 0

    for target in STORE_TARGETS:
        try:
            result = inspect_store(target, history)
            results.append({"store": target.name, **result})
            total_items += result["items_found"]
            total_changes += len(result["changes"])
        except requests.RequestException:
            logger.exception("Erro de API ao consultar a loja %s", target.name)
        except Exception:
            logger.exception("Erro inesperado ao processar a loja %s", target.name)

    history["version"] = STATE_VERSION
    save_history(history)
    return {"items_found": total_items, "changes": total_changes, "results": results, "saved": True}


def main():
    if os.getenv("TELEGRAM_SMOKE_TEST"):
        raise SystemExit(run_smoke_test())

    logger.info("Monitor iniciado | lojas=%d | cooldown=%ss", len(STORE_TARGETS), COOLDOWN)
    if not TOKEN or not CHAT_IDS:
        logger.error("TOKEN e CHAT_ID são obrigatórios para enviar alertas no Telegram")
        raise SystemExit(1)

    history = load_history()
    try:
        result = check_once(history)
        logger.info(
            "Verificação concluída: %d item(ns) encontrados, %d mudança(s), histórico salvo=%s",
            result["items_found"],
            result["changes"],
            result["saved"],
        )
        logger.info("Execução finalizada")
    except requests.RequestException:
        logger.exception("Erro da API/HTTP ao consultar o mercado")
        raise SystemExit(1)
    except Exception:
        logger.exception("Erro na execução do monitor")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
