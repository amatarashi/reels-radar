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
NEW_AUTHOR_REELS = 12       # у нового автора берём больше, чтобы понять его средние просмотры
DEEP_REELS = 24             # у сильного автора один раз проходим глубже по старым рилсам
STRONG = 8                  # с какой лучшей оценки автор считается сильным
FRESH_PER_RUN = 12          # сколько новых авторов проверять за прогон
DEEP_PER_RUN = 5            # сколько сильных авторов проходить глубоко за прогон
RELATED_PER_AUTHOR = 15     # сколько «похожих аккаунтов» брать у сильного автора
KNOWN_EVERY_HOURS = 44      # известных авторов проверяем раз в ~2 дня, даже если прогон каждый день
TARGET_REFS = 500           # пока в таблице меньше — режим набора, прогон каждый день
MAX_SCORED_PER_RUN = 300    # потолок оценок Gemini за прогон (бесплатный лимит)
MIN_SCORE = 5               # ниже — в таблицу не попадает
BUDGET_STOP_USD = 4.3       # бесплатно $5 в месяц — останавливаемся чуть раньше
MAX_VIDEO_BYTES = 14_000_000
MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hours_since(stamp):
    if not stamp:
        return 1e9
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 3600


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


def drop_author(handle, authors, refs, gone):
    """Убираем автора вместе с его рилсами и обложками. В gone — чтобы поиск похожих не вернул его снова."""
    authors.pop(handle, None)
    gone.add(handle)
    for code in [c for c, r in refs.items() if r.get("author") == handle]:
        cover = refs.pop(code).get("cover")
        if cover and (ROOT / cover).exists():
            (ROOT / cover).unlink()


def take_author_issues(authors, refs, gone):
    """Заявки «Автор: …» / «Убрать: …» из кнопок на странице. Выполняем и закрываем заявку."""
    if not (GH_TOKEN and REPO):
        return 0
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"https://api.github.com/repos/{REPO}/issues?state=open&per_page=100", headers=h, timeout=30)
    added = 0
    for issue in r.json() if r.ok else []:
        title = issue.get("title", "")
        if title.lower().startswith(("убрать", "remove")):
            handle = parse_handle(title.split(":", 1)[-1])
            if handle:
                drop_author(handle, authors, refs, gone)
            requests.post(issue["comments_url"], headers=h, json={"body": f"Убрал @{handle} и его рилсы."}, timeout=30)
            requests.patch(issue["url"], headers=h, json={"state": "closed"}, timeout=30)
            continue
        if not title.lower().startswith(("автор", "author")):
            continue
        handle = parse_handle(title.split(":", 1)[-1] + " " + (issue.get("body") or ""))
        if handle and handle not in authors:
            gone.discard(handle)
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


def apify_run(actor, payload):
    run = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/runs?token={APIFY}&waitForFinish=60",
        json=payload, timeout=90).json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        time.sleep(15)
        run = requests.get(f"https://api.apify.com/v2/actor-runs/{run['id']}?token={APIFY}", timeout=30).json()["data"]
    if run["status"] != "SUCCEEDED":
        print("Apify:", run["status"], file=sys.stderr)
    return requests.get(
        f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items?token={APIFY}&clean=true", timeout=60).json()


def apify_reels(handles, limit):
    if not handles:
        return []
    items = apify_run("apify~instagram-reel-scraper", {"username": handles, "resultsLimit": limit})
    return [i for i in items if i.get("shortCode") and not i.get("error")]


def apify_related(handles):
    """«Похожие аккаунты», которые Инстаграм показывает в профиле автора."""
    if not handles:
        return {}
    items = apify_run("apify~instagram-profile-scraper", {"usernames": handles})
    return {(i.get("username") or "").lower(): [p.get("username", "").lower() for p in i.get("relatedProfiles") or []
                                                if p.get("username") and not p.get("is_private")]
            for i in items if not i.get("error")}


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


QUOTA = "quota"  # gemini() вернёт это, когда кончился дневной лимит


def gemini(parts):
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    limited = 0
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
        limited += r.status_code == 429
        # 429 — минутный лимит бесплатного тарифа, ждём, пока он сбросится
        time.sleep(65 if r.status_code == 429 else 15)
    # все попытки упёрлись в 429 — это уже дневной лимит, ждать бесполезно
    return QUOTA if limited == 6 else None


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
    if res == QUOTA:
        return res
    if res and not res.get("talking_head", True):
        res["score"] = min(float(res.get("score", 0)), 3)
    return res


# ---------- прогон ----------

def main():
    authors = load("authors.json", {})
    refs = load("refs.json", {})
    seen = load("seen.json", {})
    state = load("state.json", {})
    gone = set(load("gone.json", []))

    added_authors = take_author_issues(authors, refs, gone)
    if "--authors-only" in sys.argv:
        save("authors.json", authors)
        save("refs.json", refs)
        save("gone.json", sorted(gone))
        print(f"Новых авторов: {added_authors}")
        return

    usage = apify_usage()
    if usage >= BUDGET_STOP_USD:
        state.update(lastRun=now(), note=f"Пауза: бесплатный лимит Apify почти исчерпан (${usage:.2f})")
        save("state.json", state)
        print(state["note"])
        return

    # набрали TARGET_REFS — по расписанию дальше работаем раз в ~2 дня, как раньше
    if (len(refs) >= TARGET_REFS and os.environ.get("GITHUB_EVENT_NAME") == "schedule"
            and hours_since(state.get("lastRun")) < KNOWN_EVERY_HOURS):
        print("Набор окончен, следующий прогон через день")
        return

    # у сильных авторов берём «похожие аккаунты» — так радар сам находит новых авторов в том же стиле
    strong = [h for h, v in authors.items() if (v.get("best") or 0) >= STRONG and not v.get("expanded")]
    for src, related in apify_related(strong).items():
        for h in [h for h in related if h not in authors and h not in gone][:RELATED_PER_AUTHOR]:
            authors[h] = {"from": f"похож на {src}", "added": now(), "hits": 0, "best": None}
    for h in strong:
        authors[h]["expanded"] = now()
    save("authors.json", authors)

    deep = [a for a, v in authors.items() if (v.get("best") or 0) >= STRONG and not v.get("deep")][:DEEP_PER_RUN]
    fresh = [a for a, v in authors.items() if not v.get("lastChecked")][:FRESH_PER_RUN]
    known = [a for a, v in authors.items() if a not in deep and v.get("lastChecked")
             and hours_since(v["lastChecked"]) >= KNOWN_EVERY_HOURS]
    items = (apify_reels(deep, DEEP_REELS) + apify_reels(fresh, NEW_AUTHOR_REELS)
             + apify_reels(known, REELS_PER_AUTHOR))
    for h in deep:
        authors[h]["deep"] = now()

    by_author = {}
    for it in items:
        by_author.setdefault(it.get("ownerUsername", "").lower(), []).append(it)

    scored = added = 0
    quota = False
    for handle, reels in by_author.items():
        if quota:
            break
        if handle in gone:
            continue
        a = authors.setdefault(handle, {"from": "найден", "added": now(), "hits": 0, "best": None})
        prev = a.get("lastChecked")
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
            if res == QUOTA:
                quota = True
                break
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
        if quota:
            # не досмотрели — пусть проверится в следующий раз
            if prev:
                a["lastChecked"] = prev
            else:
                a.pop("lastChecked")
        save("authors.json", authors)
        save("seen.json", seen)

    for handle in fresh:
        if handle not in by_author and not quota:
            authors[handle]["lastChecked"] = now()
            authors[handle]["note"] = "рилсы не найдены (закрыт или нет такого ника)"

    # найденные сами (соавторы, похожие) остаются, только если у них нашлось что-то сильное
    for handle in [h for h, v in authors.items()
                   if (v.get("from") == "найден" or v.get("from", "").startswith("похож"))
                   and (v.get("note") or (v.get("best") is not None and v["best"] < 7))]:
        authors.pop(handle)
        gone.add(handle)

    state.update(lastRun=now(), checked=len(by_author), scored=scored, added=added,
                 apifyUsd=round(apify_usage(), 2),
                 note="Дневной лимит Gemini кончился — продолжу завтра" if quota else "")
    save("authors.json", authors)
    save("refs.json", refs)
    save("seen.json", seen)
    save("state.json", state)
    save("gone.json", sorted(gone))
    print(f"Авторов: {len(by_author)}, оценено: {scored}, добавлено: {added}, Apify ${state['apifyUsd']}")


if __name__ == "__main__":
    main()
