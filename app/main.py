import datetime as dt
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


@dataclass
class Config:
    source_url: str
    play_link_host_filter: str
    play_host_prefix: str
    keywords_regex: str
    schedule_minutes: int
    tz_name: str
    output_file: Path
    timeout_seconds: int
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
        timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
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


def extract_time_href_pairs(js_text: str) -> list[tuple[str, str]]:
    lines = re.findall(r"document\.write\('([^']*)'\);", js_text)
    pairs: list[tuple[str, str]] = []
    current_time = ""

    time_re = re.compile(r'class="lab_time">([^<]+)<')
    href_re = re.compile(r'href="([^"]+)"')

    for line in lines:
        time_match = time_re.search(line)
        if time_match:
            current_time = time_match.group(1).strip()
            continue

        href_match = href_re.search(line)
        if href_match and current_time:
            href = href_match.group(1).strip()
            if href.startswith("http://") or href.startswith("https://"):
                pairs.append((current_time, href))

    return pairs


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


def filter_candidate_links(
    pairs: Iterable[tuple[str, str]], cfg: Config, now_bj: dt.datetime
) -> list[str]:
    out: list[str] = []
    for time_str, href in pairs:
        if cfg.play_link_host_filter and cfg.play_link_host_filter not in href:
            continue
        evt = parse_mmdd_hhmm_to_datetime(time_str, now_bj)
        if evt and within_3h(evt, now_bj):
            out.append(href)
    return sorted(set(out))


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


def write_tokens(path: Path, tokens: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(tokens) + ("\n" if tokens else ""), encoding="utf-8")


def read_tokens(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


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
    pairs = extract_time_href_pairs(js_text)
    candidate_links = filter_candidate_links(pairs, cfg, now_bj)

    secondary_links: list[str] = []
    for link in candidate_links:
        try:
            page = fetch_text(link, cfg.timeout_seconds)
            secondary_links.extend(extract_data_play_urls(page, cfg))
        except Exception as exc:
            print(f"[warn] open candidate failed: {link} err={exc}")

    secondary_links = sorted(set(secondary_links))

    token_set: set[str] = set()
    for url in secondary_links:
        try:
            final_page = fetch_text(url, cfg.timeout_seconds)
            token_set.update(extract_tokens(final_page))
        except Exception as exc:
            print(f"[warn] open data-play failed: {url} err={exc}")

    tokens = sorted(token_set)
    write_tokens(cfg.output_file, tokens)

    with STATE.lock:
        STATE.last_run_at = now_bj.isoformat()
        STATE.last_error = None
        STATE.last_count = len(tokens)

    print(f"[info] tokens written: {len(tokens)} -> {cfg.output_file}")


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
                "last_count": STATE.last_count,
                "output_file": str(cfg.output_file),
                "endpoints": ["/", "/healthz", "/ids", "/ids.txt", "/run-once"],
            }
        return jsonify(payload)

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    @app.get("/ids")
    def ids_json() -> Response:
        ids = read_tokens(cfg.output_file)
        return jsonify({"count": len(ids), "ids": ids})

    @app.get("/ids.txt")
    def ids_text() -> Response:
        ids = read_tokens(cfg.output_file)
        return Response("\n".join(ids) + ("\n" if ids else ""), mimetype="text/plain; charset=utf-8")

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
