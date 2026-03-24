import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify
from playwright.sync_api import sync_playwright


@dataclass
class Config:
    source_url: str
    play_link_host_filter: str
    play_host_prefix: str
    keywords_regex: str
    schedule_minutes: int
    tz_name: str
    output_file: Path
    ids_file: Path
    timeout_seconds: int
    use_browser: bool
    browser_timeout_ms: int
    host: str
    port: int


def load_config() -> Config:
    return Config(
        source_url=os.getenv("SOURCE_URL", "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5").strip(),
        play_link_host_filter=os.getenv("PLAY_LINK_HOST_FILTER", "play.sportsteam368.com").strip(),
        play_host_prefix=os.getenv("PLAY_HOST_PREFIX", "http://play.sportsteam368.com").strip(),
        keywords_regex=os.getenv("KEYWORDS_REGEX", r"高清直播|蓝光"),
        schedule_minutes=int(os.getenv("SCHEDULE_MINUTES", "10")),
        tz_name=os.getenv("TZ_NAME", "Asia/Shanghai"),
        output_file=Path(os.getenv("OUTPUT_FILE", "output/tokens.txt")),
        ids_file=Path(os.getenv("IDS_FILE", "output/ids.json")),
        timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        use_browser=os.getenv("USE_BROWSER", "false").lower() in {"1", "true", "yes", "on"},
        browser_timeout_ms=int(os.getenv("BROWSER_TIMEOUT_MS", "15000")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
    )


def now_in_tz(tz_name: str) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))
        )


def fetch_text(url: str, timeout_seconds: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=timeout_seconds)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_document_write_lines(js_text: str) -> list[str]:
    return re.findall(r"document\.write\('([^']*)'\);", js_text)


def parse_mmdd_hhmm_to_datetime(value: str, now_bj: dt.datetime) -> dt.datetime | None:
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", value)
    if not m:
        return None
    month, day, hour, minute = map(int, m.groups())

    candidates = []
    for y in (now_bj.year - 1, now_bj.year, now_bj.year + 1):
        try:
            candidates.append(
                now_bj.replace(
                    year=y,
                    month=month,
                    day=day,
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
            )
        except ValueError:
            pass

    if not candidates:
        return None

    return min(candidates, key=lambda d: abs((d - now_bj).total_seconds()))


def within_3h(event_time: dt.datetime, now_bj: dt.datetime) -> bool:
    return abs((event_time - now_bj).total_seconds()) <= 3 * 3600


def extract_match_items(js_text: str, league_prefix: str = "JRS") -> list[dict]:
    lines = extract_document_write_lines(js_text)
    items: list[dict] = []
    current: dict | None = None

    league_re = re.compile(r'class="lab_events"[^>]*><span class="name">([^<]+)</span>')
    time_re = re.compile(r'class="lab_time">([^<]+)<')
    home_re = re.compile(r'class="lab_team_home"><strong class="name">([^<]+)</strong>')
    away_re = re.compile(r'class="lab_team_away"><strong class="name">([^<]+)</strong>')
    href_re = re.compile(r'href="([^"]+)"')

    for line in lines:
        if line.startswith('<ul class="item play'):
            current = {"league": "", "time": "", "home": "", "away": "", "hrefs": []}
            continue

        if current is None:
            continue

        m = league_re.search(line)
        if m:
            current["league"] = f"{league_prefix}{m.group(1).strip()}"

        m = time_re.search(line)
        if m:
            current["time"] = m.group(1).strip()

        m = home_re.search(line)
        if m:
            current["home"] = m.group(1).strip()

        m = away_re.search(line)
        if m:
            current["away"] = m.group(1).strip()

        for hm in href_re.findall(line):
            if hm.startswith("http://") or hm.startswith("https://"):
                current["hrefs"].append(hm.strip())

        if line == "</ul>":
            if current["league"] and current["time"] and current["home"] and current["away"]:
                current["hrefs"] = sorted(set(current["hrefs"]))
                items.append(current)
            current = None

    return items


def extract_data_play_urls(page_text: str, cfg: Config) -> list[str]:
    pattern = re.compile(
        rf'<a[^>]*data-play="([^"]+)"[^>]*>\s*<em[^>]*></em>\s*<strong>([^<]*({cfg.keywords_regex})[^<]*)</strong>',
        re.IGNORECASE,
    )

    urls = []
    for m in pattern.finditer(page_text):
        data_play = m.group(1).strip()
        full_url = urljoin(cfg.play_host_prefix.rstrip("/") + "/", data_play.lstrip("/"))
        urls.append(full_url)

    return sorted(set(urls))


def extract_tokens(final_page: str) -> list[str]:
    tokens: list[str] = []
    tokens.extend(re.findall(r"var\s+encodedStr\s*=\s*'([^']+)'", final_page))
    tokens.extend(re.findall(r"paps\.html\?id=([^'\"&\s]+)", final_page))
    return sorted(set(tokens))


def extract_tokens_with_browser(url: str, cfg: Config) -> list[str]:
    token_set: set[str] = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        def collect_from_url(raw: str) -> None:
            token_set.update(re.findall(r"paps\.html\?id=([^'\"&\s]+)", raw))

        page.on("request", lambda req: collect_from_url(req.url))
        page.on("response", lambda resp: collect_from_url(resp.url))

        page.goto(url, wait_until="networkidle", timeout=cfg.browser_timeout_ms)
        collect_from_url(page.content())
        collect_from_url(page.url)

        resource_urls = page.evaluate(
            "() => performance.getEntriesByType('resource').map(r => r.name)"
        )
        if isinstance(resource_urls, list):
            for resource_url in resource_urls:
                if isinstance(resource_url, str):
                    collect_from_url(resource_url)

        browser.close()

    return sorted(token_set)


def write_tokens(path: Path, tokens: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(tokens) + ("\n" if tokens else ""), encoding="utf-8")


def read_tokens(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_ids(path: Path, ids: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")


def read_ids(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last_count: int = 0


STATE = AppState()


def run_once(cfg: Config) -> None:
    if not cfg.source_url:
        raise ValueError("SOURCE_URL is empty")
    if not cfg.play_host_prefix:
        raise ValueError("PLAY_HOST_PREFIX is empty")

    now_bj = now_in_tz(cfg.tz_name)
    js_text = fetch_text(cfg.source_url, cfg.timeout_seconds)

    raw_items = extract_match_items(js_text, league_prefix="JRS")

    # 过滤到北京时间前后3小时的比赛，并建立 match_link -> match_meta 映射
    match_links: list[tuple[str, dict]] = []
    for item in raw_items:
        evt = parse_mmdd_hhmm_to_datetime(item["time"], now_bj)
        if not evt or not within_3h(evt, now_bj):
            continue
        meta = {
            "league": item["league"],
            "time": item["time"],
            "home": item["home"],
            "away": item["away"],
        }
        for href in item["hrefs"]:
            if cfg.play_link_host_filter and cfg.play_link_host_filter not in href:
                continue
            match_links.append((href, meta))

    # 候选页面 -> data-play
    data_play_tasks: list[tuple[str, dict]] = []
    seen_pair = set()
    for href, meta in match_links:
        try:
            page = fetch_text(href, cfg.timeout_seconds)
            for dp in extract_data_play_urls(page, cfg):
                key = (dp, meta["league"], meta["time"], meta["home"], meta["away"])
                if key not in seen_pair:
                    seen_pair.add(key)
                    data_play_tasks.append((dp, meta))
        except Exception as exc:
            print(f"[warn] open candidate failed: {href} err={exc}")

    # data-play -> ids，并与比赛信息一一对应
    mapped_ids: list[dict] = []
    seen_mapped = set()
    token_only: set[str] = set()

    for dp_url, meta in data_play_tasks:
        try:
            if cfg.use_browser:
                extracted_ids = extract_tokens_with_browser(dp_url, cfg)
            else:
                final_page = fetch_text(dp_url, cfg.timeout_seconds)
                extracted_ids = extract_tokens(final_page)

            for token in extracted_ids:
                token_only.add(token)
                row = {
                    "id": token,
                    "league": meta["league"],
                    "time": meta["time"],
                    "home": meta["home"],
                    "away": meta["away"],
                }
                sk = (row["id"], row["league"], row["time"], row["home"], row["away"])
                if sk not in seen_mapped:
                    seen_mapped.add(sk)
                    mapped_ids.append(row)
        except Exception as exc:
            print(f"[warn] open data-play failed: {dp_url} err={exc}")

    mapped_ids.sort(key=lambda x: (x["time"], x["league"], x["home"], x["away"], x["id"]))
    write_ids(cfg.ids_file, mapped_ids)
    write_tokens(cfg.output_file, sorted(token_only))

    with STATE.lock:
        STATE.last_run_at = now_bj.isoformat()
        STATE.last_error = None
        STATE.last_count = len(mapped_ids)

    print(f"[info] mapped ids={len(mapped_ids)} -> {cfg.ids_file}")


def scheduler_loop(cfg: Config) -> None:
    while True:
        try:
            run_once(cfg)
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = str(exc)
            print(f"[error] {exc}")

        sleep_seconds = max(cfg.schedule_minutes, 1) * 60
        print(f"[info] sleep {sleep_seconds}s")
        time.sleep(sleep_seconds)


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        with STATE.lock:
            payload = {
                "status": "running",
                "last_run_at": STATE.last_run_at,
                "last_error": STATE.last_error,
                "mapped_id_count": STATE.last_count,
                "ids_file": str(cfg.ids_file),
                "tokens_file": str(cfg.output_file),
                "use_browser": cfg.use_browser,
                "endpoints": ["/", "/healthz", "/ids", "/ids.txt", "/run-once"],
            }
        return jsonify(payload)

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    @app.get("/ids")
    def ids_json() -> Response:
        data = read_ids(cfg.ids_file)
        return jsonify({"count": len(data), "items": data})

    @app.get("/ids.txt")
    def ids_text() -> Response:
        data = read_ids(cfg.ids_file)
        lines = [f'{i["league"]}|{i["time"]}|{i["home"]} vs {i["away"]}|{i["id"]}' for i in data]
        return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain; charset=utf-8")

    @app.post("/run-once")
    def trigger_once() -> Response:
        threading.Thread(target=run_once, args=(cfg,), daemon=True).start()
        return jsonify({"queued": True})

    return app


def main() -> None:
    cfg = load_config()

    thread = threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True)
    thread.start()

    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
