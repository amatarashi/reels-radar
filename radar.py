"""Радар референсов: берёт свежие рилсы авторов через Apify, оценивает их Gemini по профилю стиля,
подходящие складывает в data/refs.json для таблицы на GitHub Pages."""
import base64
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
COVERS = ROOT / "covers"
APIFY = os.environ.get("APIFY_TOKEN", "")
GEMINI = os.environ.get("GEMINI_API_KEY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")

REELS_PER_AUTHOR = 4        # сколько последних рилсов брать у автора за прогон
NEW_AUTHOR_REELS = 5        # у нового автора берём больше, чтобы понять его средние просмотры
MAX_SCORED_PER_RUN = 150    # потолок оценок Gemini за прогон (бесплатный лимит)
MIN_SCORE = 5               # ниже — в таблицу не попадает
BUDGET_STOP_USD = 4.3       # бесплатно $5 в месяц — останавливаемся чуть раньше
MAX_VIDEO_BYTES = 14_000_000
MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- авторы из заявок (issues) ----------

HANDLE_RE = re.compile(r"(?:instagram\.com/|@)?([A-Za-z0-9._]{2,30})")


def parse_handle(text):
    text = text.strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]{2,30})", text) or re.search(r"@([A-Za-z0-9._]{2,30})", text)
    if m:
        return m.group(1).lower().strip(".")
    m = HANDLE_RE.fullmatch(text.split()[-1]) if text else None
    return m.group(1).lower().strip(".") if m else None


def take_author_issues(authors):
    """Заявки «Автор: …» из кнопки на странице. Добавляем автора и закрываем заявку."""
    if not (GH_TOKEN and REPO):
        return 0
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"https://api.github.com/repos/{REPO}/issues?state=open&per_page=100", headers=h, timeout=30)
    added = 0
    for issue in r.json() if r.ok else []:
        title = issue.get("title", "")
        if title.lower().startswith(("убрать", "remove")):
            handle = parse_handle(title.split(":", 1)[-1])
            if handle in authors:
                del authors[handle]
            requests.post(issue["comments_url"], headers=h, json={"body": f"Убрал @{handle}."}, timeout=30)
            requests.patch(issue["url"], headers=h, json={"state": "closed"}, timeout=30)
            continue
        if not title.lower().startswith(("автор", "author")):
            continue
        handle = parse_handle(title.split(":", 1)[-1] + " " + (issue.get("body") or ""))
        if handle and handle not in authors:
            authors[handle] = {"from": "добавлен через страницу", "added": now(), "hits": 0, "best": None}
            added += 1
            msg = f"Добавил @{handle}, проверю на следующем прогоне."
        else:
            msg = "Не понял ник или автор уже есть." if not handle else f"@{handle} уже в списке."
        requests.post(issue["comments_url"], headers=h, json={"body": msg}, timeout=30)
        requests.patch(issue["url"], headers=h, json={"state": "closed"}, timeout=30)
    return added


# ---------- Apify ----------

def apify_usage():
    try:
        r = requests.get(f"https://api.apify.com/v2/users/me/limits?token={APIFY}", timeout=30).json()["data"]
        return float(r["current"]["monthlyUsageUsd"])
    except Exception:
        return 0.0


def apify_reels(handles, limit):
    if not handles:
        return []
    run = requests.post(
        f"https://api.apify.com/v2/acts/apify~instagram-reel-scraper/runs?token={APIFY}&waitForFinish=60",
        json={"username": handles, "resultsLimit": limit}, timeout=90).json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        time.sleep(15)
        run = requests.get(f"https://api.apify.com/v2/actor-runs/{run['id']}?token={APIFY}", timeout=30).json()["data"]
    if run["status"] != "SUCCEEDED":
        print("Apify:", run["status"], file=sys.stderr)
    items = requests.get(
        f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items?token={APIFY}&clean=true", timeout=60).json()
    return [i for i in items if i.get("shortCode") and not i.get("error")]


# ---------- Gemini ----------

STYLE = (ROOT / "style.md").read_text(encoding="utf-8")
PROMPT = STYLE + """

Тебе дан рилс из Инстаграма (видео целиком или, если видео нет, обложка) и его подпись.
Оцени его как референс для монтажа по профилю выше. Будь строг к формату:
если это не «один человек говорит в камеру большую часть ролика» — talking_head=false и score не выше 3.
Ответь строго JSON:
{"talking_head": true/false,
 "score": число 0-10 (можно с .5),
 "hook": "первая фраза или заголовок ролика по-русски, одно ключевое слово обёрни в *звёздочки*",
 "why": "одно-два предложения: чем ролик полезен как референс или почему мимо",
 "tricks": ["2-5 коротких приёмов монтажа, которые видны в ролике"]}
"""


def gemini(parts):
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    for attempt in range(6):
        model = MODELS[attempt % len(MODELS)]
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": GEMINI}, json=body, timeout=240)
        except requests.RequestException as e:
            print(f"Gemini {model}: {e!r}", file=sys.stderr)
            continue
        if r.ok:
            try:
                text = r.json()["candidates"][0]["content"]["parts"][-1]["text"]
                return json.loads(text[text.find("{"): text.rfind("}") + 1])
            except (KeyError, IndexError, ValueError):
                print(f"Gemini {model}: непонятный ответ", file=sys.stderr)
                continue
        print(f"Gemini {model}: {r.status_code}", file=sys.stderr)
        # 429 — минутный лимит бесплатного тарифа, ждём, пока он сбросится
        time.sleep(65 if r.status_code == 429 else 15)
    return None


def fetch(url, cap):
    try:
        r = requests.get(url, timeout=60, stream=True)
        if not r.ok:
            return None
        data = b""
        for chunk in r.iter_content(1 << 16):
            data += chunk
            if len(data) > cap:
                return None
        return data
    except requests.RequestException:
        return None


def score_reel(item):
    caption = (item.get("caption") or "")[:1500]
    video = fetch(item["videoUrl"], MAX_VIDEO_BYTES) if item.get("videoUrl") else None
    if video:
        media = {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(video).decode()}}
    else:
        img = fetch(item.get("displayUrl", ""), 5_000_000)
        if not img:
            return None
        media = {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}
    res = gemini([media, {"text": PROMPT + f"\n\nПодпись к рилсу:\n{caption}"}])
    if res and not res.get("talking_head", True):
        res["score"] = min(float(res.get("score", 0)), 3)
    return res


# ---------- прогон ----------

def main():
    authors = load("authors.json", {})
    refs = load("refs.json", {})
    seen = load("seen.json", {})
    state = load("state.json", {})

    added_authors = take_author_issues(authors)
    if "--authors-only" in sys.argv:
        save("authors.json", authors)
        print(f"Новых авторов: {added_authors}")
        return

    usage = apify_usage()
    if usage >= BUDGET_STOP_USD:
        state.update(lastRun=now(), note=f"Пауза: бесплатный лимит Apify почти исчерпан (${usage:.2f})")
        save("state.json", state)
        print(state["note"])
        return

    fresh = [a for a, v in authors.items() if not v.get("lastChecked")]
    known = [a for a in authors if a not in fresh]
    items = apify_reels(fresh, NEW_AUTHOR_REELS) + apify_reels(known, REELS_PER_AUTHOR)

    by_author = {}
    for it in items:
        by_author.setdefault(it.get("ownerUsername", "").lower(), []).append(it)

    scored = added = 0
    for handle, reels in by_author.items():
        a = authors.setdefault(handle, {"from": "найден", "added": now(), "hits": 0, "best": None})
        a["lastChecked"] = now()
        views = [r.get("videoPlayCount") or r.get("videoViewCount") or 0 for r in reels]
        a["views"] = sorted(set(a.get("views", []) + views))[-30:]
        median = statistics.median(a["views"]) if a["views"] else 0
        for r in reels:
            code = r["shortCode"]
            if code in seen or code in refs or scored >= MAX_SCORED_PER_RUN:
                continue
            v = r.get("videoPlayCount") or r.get("videoViewCount") or 0
            try:
                res = score_reel(r)
            except Exception as e:
                print(f"{code}: {e!r}", file=sys.stderr)
                res = None
            scored += 1
            time.sleep(15)
            if not res:
                continue
            score = float(res.get("score", 0))
            seen[code] = {"author": handle, "score": score, "at": now()}
            a["best"] = max(a.get("best") or 0, score)
            if score < MIN_SCORE:
                continue
            cover = fetch(r.get("displayUrl", ""), 3_000_000)
            if cover:
                COVERS.mkdir(exist_ok=True)
                (COVERS / f"{code}.jpg").write_bytes(cover)
            refs[code] = {
                "url": f"https://www.instagram.com/reel/{code}/", "author": handle,
                "hook": res.get("hook", ""), "score": score, "why": res.get("why", ""),
                "tricks": res.get("tricks", [])[:5], "views": v, "aboveMedian": v >= median,
                "duration": round(r.get("videoDuration") or 0), "posted": r.get("timestamp"),
                "found": now(), "cover": f"covers/{code}.jpg" if cover else None,
            }
            a["hits"] = a.get("hits", 0) + 1
            added += 1
            save("refs.json", refs)
        save("authors.json", authors)
        save("seen.json", seen)

    for handle in fresh:
        if handle not in by_author:
            authors[handle]["lastChecked"] = now()
            authors[handle]["note"] = "рилсы не найдены (закрыт или нет такого ника)"

    # соавторы чужих рилсов попадают сюда сами — оставляем только тех, у кого нашлось что-то сильное
    for handle in [h for h, v in authors.items() if v.get("from") == "найден" and (v.get("best") or 0) < 7]:
        del authors[handle]

    state.update(lastRun=now(), checked=len(by_author), scored=scored, added=added,
                 apifyUsd=round(apify_usage(), 2), note="")
    save("authors.json", authors)
    save("refs.json", refs)
    save("seen.json", seen)
    save("state.json", state)
    print(f"Авторов: {len(by_author)}, оценено: {scored}, добавлено: {added}, Apify ${state['apifyUsd']}")


if __name__ == "__main__":
    main()
