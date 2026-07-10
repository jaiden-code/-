#!/usr/bin/env python3
import argparse
import email.message
import html
import http.client
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PRICE_RE = re.compile(r"(?:(?:US)?\$|USD\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
PRODUCT_NAME_RE = re.compile(r'<h5\s+class=["\']product_name["\']\s*>(.*?)</h5>', re.I | re.S)
CURRENT_PRICE_RE = re.compile(r'<span\s+class=["\']current_price["\']\s*>(.*?)</span>', re.I | re.S)
BOX_RE = re.compile(r"<li>\s*(Box\s*[^<]+)\s*</li>", re.I | re.S)
ADDCART_RE = re.compile(r"addcart\.php\?ptype=cigars&amp;pid=([0-9]+)|addcart\.php\?ptype=cigars&pid=([0-9]+)", re.I)
COH_PRODUCT_HEADER_RE = re.compile(r'<span\s+class=["\']product_header["\']\s*>(.*?)</span>', re.I | re.S)
COH_CART_RE = re.compile(r"AddToCart\.aspx\?([^\"]+)", re.I | re.S)
COH_PRICE_RE = re.compile(r'class=["\']pricetxt["\'][^>]*>.*?<strong>(.*?)</strong>', re.I | re.S)


@dataclass
class Link:
    text: str
    url: str


class LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[Link] = []
        self._href_stack: list[str | None] = []
        self._text_stack: list[list[str]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            self._href_stack.append(href)
            self._text_stack.append([])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a" and self._href_stack:
            href = self._href_stack.pop()
            parts = self._text_stack.pop()
            text = clean_text(" ".join(parts))
            if href and text:
                self.links.append(Link(text=text, url=urllib.parse.urljoin(self.base_url, href)))

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._text_stack:
            self._text_stack[-1].append(data)


def clean_text(value: str) -> str:
    return SPACE_RE.sub(" ", html.unescape(value or "")).strip()


def strip_tags(value: str) -> str:
    return clean_text(TAG_RE.sub(" ", value or ""))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def fetch(url: str, user_agent: str, timeout: int = 60) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Fetch failed without an exception: {url}") from last_error


def normalize_id(name: str, url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/").lower()
    if path and path != "/":
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
    return clean_text(name).lower()


def extract_price(text: str) -> str | None:
    match = PRICE_RE.search(text)
    if not match:
        return None
    return match.group(0).replace(" ", "")


def is_product_link(link: Link, target: dict[str, Any]) -> bool:
    text_lower = link.text.lower()
    url_lower = link.url.lower()

    if len(link.text) < 4 or len(link.text) > 180:
        return False

    for word in target.get("exclude_keywords", []):
        if word.lower() in text_lower or word.lower() in url_lower:
            return False

    product_url_contains = [x.lower() for x in target.get("product_url_contains", [])]
    include_keywords = [x.lower() for x in target.get("include_keywords", [])]

    url_match = any(x in url_lower for x in product_url_contains)
    text_match = any(x in text_lower for x in include_keywords)

    if not product_url_contains and not include_keywords:
        return True
    return url_match or text_match


def extract_ihavanas_products(page_html: str, page_url: str) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    matches = list(PRODUCT_NAME_RE.finditer(page_html))
    for idx, match in enumerate(matches):
        name = strip_tags(match.group(1))
        if not name:
            continue

        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page_html)
        block = page_html[match.end() : end]
        addcart = ADDCART_RE.search(block)
        if not addcart:
            continue

        product_pid = addcart.group(1) or addcart.group(2)
        box_match = BOX_RE.search(block)
        price_match = CURRENT_PRICE_RE.search(block)
        box = strip_tags(box_match.group(1)) if box_match else None
        price = strip_tags(price_match.group(1)).replace("US$ ", "US$").replace("US$ ", "US$") if price_match else None
        full_name = f"{name} ({box})" if box else name
        item_id = f"ihavanas:{product_pid}"
        products[item_id] = {
            "id": item_id,
            "name": full_name,
            "url": page_url,
            "price": price,
        }
    return products


def extract_cohcigars_products(page_html: str, page_url: str) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    source = html.unescape(page_html)
    matches = list(COH_PRODUCT_HEADER_RE.finditer(source))
    for idx, match in enumerate(matches):
        header_name = strip_tags(match.group(1)).lstrip("\xa0").strip()
        if not header_name:
            continue

        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(source)
        block = source[match.end() : end]
        for cart_match in COH_CART_RE.finditer(block):
            query = urllib.parse.parse_qs(cart_match.group(1), keep_blank_values=True)
            prid = (query.get("prid") or [""])[0]
            bxid = (query.get("bxid") or [""])[0]
            if not prid or not bxid:
                continue

            box = clean_text((query.get("bx") or [""])[0])
            stock = clean_text((query.get("pstk") or [""])[0])
            row_start = block.rfind("<tr", 0, cart_match.start())
            row_end = block.find("</tr>", cart_match.end())
            row = block[row_start:row_end] if row_start >= 0 and row_end >= 0 else block[: cart_match.end()]
            price_match = COH_PRICE_RE.search(row)
            price = strip_tags(price_match.group(1)) if price_match else None
            full_name = f"{header_name} ({box})" if box else header_name
            item_id = f"cohcigars:{prid}:{bxid}"
            products[item_id] = {
                "id": item_id,
                "name": full_name,
                "url": page_url,
                "price": price,
                "stock": stock or None,
            }
    return products


def extract_products(page_html: str, target: dict[str, Any], page_url: str) -> dict[str, dict[str, Any]]:
    if "cohcigars.com" in urllib.parse.urlsplit(page_url).netloc.lower():
        return extract_cohcigars_products(page_html, page_url)

    structured_products = extract_ihavanas_products(page_html, page_url)
    if "ihavanas.com" in urllib.parse.urlsplit(page_url).netloc.lower():
        return structured_products

    parser = LinkParser(page_url)
    parser.feed(page_html)

    products: dict[str, dict[str, Any]] = {}
    for link in parser.links:
        if not is_product_link(link, target):
            continue
        item_id = normalize_id(link.text, link.url)
        price = extract_price(link.text)
        existing = products.get(item_id)
        item = {
            "id": item_id,
            "name": link.text,
            "url": link.url,
            "price": price,
        }
        if existing is None or len(item["name"]) > len(existing.get("name", "")):
            products[item_id] = item
    return products


def diff_products(
    target: dict[str, Any],
    previous: dict[str, dict[str, Any]],
    ever_seen: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_ids = set(previous)
    current_ids = set(current)
    ever_ids = set(ever_seen)

    for item_id in sorted(current_ids - ever_ids):
        if target.get("notify_on_new", True):
            events.append({"type": "new", "item": current[item_id]})

    for item_id in sorted((current_ids & ever_ids) - previous_ids):
        if target.get("notify_on_restock", True):
            events.append({"type": "restock", "item": current[item_id]})

    if target.get("notify_on_price_change", True):
        for item_id in sorted(current_ids & previous_ids):
            old_price = previous[item_id].get("price")
            new_price = current[item_id].get("price")
            if old_price and new_price and old_price != new_price:
                events.append(
                    {
                        "type": "price_change",
                        "item": current[item_id],
                        "old_price": old_price,
                        "new_price": new_price,
                    }
                )

    if target.get("notify_on_stock_change", True):
        for item_id in sorted(current_ids & previous_ids):
            old_stock = previous[item_id].get("stock")
            new_stock = current[item_id].get("stock")
            if old_stock and new_stock and old_stock.isdigit() and new_stock.isdigit():
                if int(new_stock) > int(old_stock):
                    events.append(
                        {
                            "type": "stock_increase",
                            "item": current[item_id],
                            "old_stock": old_stock,
                            "new_stock": new_stock,
                        }
                    )

    return events


def event_title(event_type: str) -> str:
    return {
        "new": "雪茄新品上架",
        "restock": "雪茄库存补给",
        "price_change": "雪茄价格变化",
        "stock_increase": "雪茄库存增加",
    }.get(event_type, "雪茄监测提醒")


def format_event(target_name: str, event: dict[str, Any]) -> tuple[str, str]:
    item = event["item"]
    title = event_title(event["type"])
    lines = [
        f"{title} - {target_name}",
        f"商品: {item.get('name', '')}",
    ]
    if event["type"] == "price_change":
        lines.append(f"价格: {event.get('old_price')} -> {event.get('new_price')}")
    elif event["type"] == "stock_increase":
        lines.append(f"库存: {event.get('old_stock')} -> {event.get('new_stock')}")
        if item.get("price"):
            lines.append(f"价格: {item['price']}")
    elif item.get("price"):
        lines.append(f"价格: {item['price']}")
    if item.get("stock") and event["type"] != "stock_increase":
        lines.append(f"库存: {item['stock']}")
    lines.append(f"链接: {item.get('url', '')}")
    lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return title, "\n".join(lines)


def format_event_summary(target_name: str, event: dict[str, Any], index: int) -> str:
    item = event["item"]
    lines = [
        f"{index}. {event_title(event['type'])}",
        f"网站: {target_name}",
        f"商品: {item.get('name', '')}",
    ]
    if event["type"] == "price_change":
        lines.append(f"价格: {event.get('old_price')} -> {event.get('new_price')}")
    elif event["type"] == "stock_increase":
        lines.append(f"库存: {event.get('old_stock')} -> {event.get('new_stock')}")
        if item.get("price"):
            lines.append(f"价格: {item['price']}")
    elif item.get("price"):
        lines.append(f"价格: {item['price']}")
    if item.get("stock") and event["type"] != "stock_increase":
        lines.append(f"库存: {item['stock']}")
    lines.append(f"链接: {item.get('url', '')}")
    return "\n".join(lines)


def format_digest(events: list[tuple[str, dict[str, Any]]]) -> tuple[str, str]:
    title = f"雪茄监测提醒：{len(events)} 条更新"
    lines = [
        title,
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for index, (target_name, event) in enumerate(events, start=1):
        lines.append(format_event_summary(target_name, event, index))
        lines.append("")
    return title, "\n".join(lines).rstrip()


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def notify_github_issue(title: str, message: str, config: dict[str, Any]) -> None:
    github_cfg = config.get("notifications", {}).get("github_issue", {})
    if not github_cfg.get("enabled"):
        return

    token = os.environ.get("GITHUB_TOKEN") or github_cfg.get("token", "")
    repository = os.environ.get("GITHUB_REPOSITORY") or github_cfg.get("repository", "")
    if not token or not repository:
        raise RuntimeError("GitHub issue notification is enabled but GITHUB_TOKEN or GITHUB_REPOSITORY is missing.")

    mention = github_cfg.get("mention", "").strip()
    body = f"{mention}\n\n{message}".strip() if mention else message
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
    }
    labels = github_cfg.get("labels", [])
    if labels:
        payload["labels"] = labels

    post_json(
        f"https://api.github.com/repos/{repository}/issues",
        payload,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def notify(config: dict[str, Any], title: str, message: str) -> None:
    notifications = config.get("notifications", {})
    delivery_errors: list[str] = []
    delivered = False

    if notifications.get("console", {}).get("enabled", True):
        print("\n" + "=" * 72)
        print(message)
        print("=" * 72)

    tg = notifications.get("telegram", {})
    if tg.get("enabled"):
        try:
            token = tg["bot_token"]
            chat_id = tg["chat_id"]
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            post_json(url, {"chat_id": chat_id, "text": message, "disable_web_page_preview": False})
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"Telegram: {exc}")

    bark = notifications.get("bark", {})
    if bark.get("enabled"):
        try:
            endpoint = bark["endpoint"].rstrip("/")
            data = urllib.parse.urlencode({"title": title, "body": message}).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=30):
                pass
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"Bark: {exc}")

    pushplus = notifications.get("pushplus", {})
    if pushplus.get("enabled"):
        try:
            post_json(
                "https://www.pushplus.plus/send",
                {"token": pushplus["token"], "title": title, "content": message},
            )
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"PushPlus: {exc}")

    server_chan = notifications.get("server_chan", {})
    if server_chan.get("enabled"):
        try:
            sendkey = server_chan["sendkey"]
            post_json(f"https://sctapi.ftqq.com/{sendkey}.send", {"title": title, "desp": message})
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"ServerChan: {exc}")

    webhook = notifications.get("webhook", {})
    if webhook.get("enabled"):
        try:
            post_json(webhook["url"], {"title": title, "message": message}, webhook.get("headers", {}))
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"Webhook: {exc}")

    smtp_cfg = notifications.get("smtp", {})
    if smtp_cfg.get("enabled"):
        try:
            smtp_host = os.environ.get("SMTP_HOST") or smtp_cfg["host"]
            smtp_port = int(os.environ.get("SMTP_PORT") or smtp_cfg.get("port", 587))
            smtp_username = os.environ.get("SMTP_USERNAME") or smtp_cfg["username"]
            smtp_password = os.environ.get("SMTP_PASSWORD") or smtp_cfg["password"]
            smtp_from = os.environ.get("SMTP_FROM") or smtp_cfg["from"]
            smtp_to = os.environ.get("SMTP_TO") or smtp_cfg["to"]
            if not smtp_password:
                raise RuntimeError("SMTP password is empty. Set SMTP_PASSWORD in the cloud secret or config.json.")

            msg = email.message.EmailMessage()
            msg["Subject"] = title
            msg["From"] = smtp_from
            msg["To"] = smtp_to
            msg.set_content(message)
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls(context=context)
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"SMTP: {exc}")

    github_issue_cfg = notifications.get("github_issue", {})
    if github_issue_cfg.get("enabled") and (not github_issue_cfg.get("fallback_only", True) or not delivered):
        try:
            notify_github_issue(title, message, config)
            delivered = True
        except Exception as exc:
            delivery_errors.append(f"GitHub issue: {exc}")

    if delivery_errors:
        print("Notification warning: " + " | ".join(delivery_errors), file=sys.stderr)

    active_channels = [
        name
        for name, channel in notifications.items()
        if name != "console" and isinstance(channel, dict) and channel.get("enabled")
    ]
    if active_channels and not delivered:
        raise RuntimeError("All non-console notification channels failed: " + " | ".join(delivery_errors))


def check_once(config: dict[str, Any], config_path: Path) -> int:
    state_path = Path(config.get("state_file", "data/state.json"))
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path
    state = load_json(state_path, {"targets": {}})
    total_events = 0
    digest_events: list[tuple[str, dict[str, Any]]] = []

    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue

        name = target.get("name") or target["url"]
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking {name}")

        target_state = state["targets"].setdefault(name, {"previous": {}, "ever_seen": {}, "last_checked": None})
        previous = target_state.get("previous", {})
        ever_seen = target_state.get("ever_seen", {})

        target_urls = target.get("urls") or [target.get("url")]
        current: dict[str, dict[str, Any]] = {}
        page_errors: list[str] = []
        for page_url in target_urls:
            if not page_url:
                continue
            page_target = {**target, "url": page_url}
            try:
                page_html = fetch(
                    page_url,
                    config.get("user_agent", "Mozilla/5.0"),
                    int(config.get("request_timeout_seconds", 60)),
                )
                current.update(extract_products(page_html, page_target, page_url))
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                page_errors.append(f"{page_url}: {exc}")

        if page_errors:
            target_state["last_checked"] = now_iso()
            target_state["last_error"] = "; ".join(page_errors[:5])
            if len(page_errors) > 5:
                target_state["last_error"] += f"; and {len(page_errors) - 5} more"
            print(f"Skipped state update for {name}: {len(page_errors)} page error(s).")
            continue
        events = diff_products(target, previous, ever_seen, current)
        for event in events:
            digest_events.append((name, event))
        total_events += len(events)

        target_state["previous"] = current
        target_state["ever_seen"] = {**ever_seen, **current}
        target_state["last_checked"] = now_iso()
        target_state["last_count"] = len(current)
        target_state["last_error"] = None

    if digest_events:
        title, message = format_digest(digest_events)
        notify(config, title, message)

    save_json(state_path, state)
    return total_events


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor cigar websites for new or restocked products.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument("--init-state", action="store_true", help="Record current products without sending alerts.")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Copy config.example.json to config.json first.")
        return 2

    config = load_json(config_path, {})

    if args.init_state:
        old_notifications = config.get("notifications", {})
        config["notifications"] = {"console": {"enabled": False}}
        check_once(config, config_path)
        config["notifications"] = old_notifications
        print("Initial state saved. Future runs will only alert on changes.")
        return 0

    while True:
        try:
            events = check_once(config, config_path)
            if events == 0:
                print("No changes.")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"Check failed: {exc}", file=sys.stderr)

        if args.once:
            return 0
        time.sleep(int(config.get("check_interval_seconds", 300)))


if __name__ == "__main__":
    raise SystemExit(main())
