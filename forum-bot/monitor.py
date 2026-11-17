#!/usr/bin/env python3
"""
Бот-наблюдатель за законами на форуме forum.gta5rp.com.

Что делает:
  1. Скачивает все темы из sources.txt (по одной, с паузами; соблюдает robots.txt).
  2. Разбирает их тем же кодом, которым была собрана база сайта.
  3. Сравнивает с тем, что сейчас зашито в index.html (по статьям).
  4. Пишет понятный отчёт (report.md): что добавили, убрали, изменили (было/стало).
  5. С флагом --apply обновляет базу внутри index.html и поднимает версию базы,
     чтобы игроки увидели «База законов обновлена».

Защита от порчи данных: если тема не скачалась, пришла пустой или из неё пропала
большая часть статей, эта тема НЕ применяется, а попадает в отчёт «проверьте вручную».

Запуск:
    python monitor.py --site ../index.html                # только проверить и показать
    python monitor.py --site ../index.html --apply        # проверить и обновить index.html
    python monitor.py --site ../index.html --offline папка_с_сохранёнными_страницами
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scrape_docossum import get, make_session
from scrape_forum import FORUM, index_saved, load_sources, parse_thread

DATA_MARK = "var D="
MIN_KEEP = 0.6          # новая версия темы должна сохранить не меньше 60% статей


def load_site(path):
    html = Path(path).read_text(encoding="utf-8")
    i = html.index(DATA_MARK) + len(DATA_MARK)
    data, end = json.JSONDecoder().raw_decode(html[i:])
    return html, data, i, i + end


def dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def keyed(items):
    """Ключ статьи: её номер, а если номера нет, то глава и заголовок. Повторы нумеруем."""
    out, seen = {}, {}
    for a in items:
        k = a["n"] if a["n"] else "t:" + (a.get("c") or "") + "|" + (a.get("t") or "")
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            k = f"{k}#{seen[k]}"
        out[k] = a
    return out


def word_diff(old, new, limit=900):
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            seg = a[i1:i2]
            parts.append(" ".join(seg) if len(seg) <= 14 else " ".join(seg[:6]) + " … " + " ".join(seg[-6:]))
        else:
            if i2 > i1:
                parts.append("~~" + " ".join(a[i1:i2]) + "~~")
            if j2 > j1:
                parts.append("**" + " ".join(b[j1:j2]) + "**")
    s = " ".join(parts)
    return s if len(s) <= limit else s[:limit] + " …"


class BrowserFetcher:
    """Запасной способ: настоящий Chromium (проходит часть защит, которые режут обычные запросы)."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._br = self._pw.chromium.launch()
        self._ctx = self._br.new_context(locale="ru-RU", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"))

    def get(self, url):
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            try:
                page.wait_for_selector(".bbWrapper", timeout=25000)
            except Exception:
                raise RuntimeError("страница не содержит текста темы (защита от ботов или вход)")
            return page.content()
        finally:
            page.close()

    def close(self):
        self._br.close()
        self._pw.stop()


def to_site_articles(articles):
    return [{"c": x["chapter"], "n": x["number"], "t": x["title"], "x": x["text"]} for x in articles]


def human_date(iso):
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return None


def compare(old_arts, new_arts):
    o, n = keyed(old_arts), keyed(new_arts)
    added = [n[k] for k in n if k not in o]
    removed = [o[k] for k in o if k not in n]
    changed = [(o[k], n[k]) for k in n if k in o and (o[k]["x"], o[k]["c"], o[k]["t"]) != (n[k]["x"], n[k]["c"], n[k]["t"])]
    return added, removed, changed


def label(a):
    return ("Статья " + a["n"]) if a["n"] else (a["t"] or "(без номера)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default="../index.html")
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--delay", type=float, default=4.0, help="пауза между темами, сек.")
    ap.add_argument("--apply", action="store_true", help="записать изменения в index.html")
    ap.add_argument("--offline", help="папка с сохранёнными страницами (для проверки без сети)")
    ap.add_argument("--browser", action="store_true", help="качать через Chromium (если обычные запросы блокируются)")
    args = ap.parse_args()

    html, data, s0, s1 = load_site(args.site)
    by_id = {d["id"]: d for d in data}
    items = load_sources(args.sources)
    if not items:
        sys.exit("В sources.txt нет ссылок.")

    session = make_session()
    saved = index_saved([args.offline]) if args.offline else {}
    bf = BrowserFetcher() if (args.browser and not args.offline) else None
    if not args.offline and not bf:
        try:
            from urllib import robotparser
            rp = robotparser.RobotFileParser()
            r = session.get(FORUM + "/robots.txt", timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                if not rp.can_fetch("*", items[0]["url"]):
                    sys.exit("robots.txt форума запрещает автоматическое скачивание. Остановлено.")
        except Exception:
            pass

    changes, failed, suspicious, unchanged = [], [], [], 0
    applied = []
    for n, it in enumerate(items, 1):
        d = by_id.get(it["id"])
        name = it["title"] or it["id"]
        if d is None:
            failed.append((name, "темы нет в базе сайта (новая тема: добавьте её через полную сборку)"))
            continue
        try:
            if args.offline:
                page = saved.get(it["id"])
                if not page:
                    raise RuntimeError("нет сохранённой страницы")
            else:
                page = bf.get(it["url"]) if bf else get(session, it["url"], delay=args.delay)
                time.sleep(args.delay)
            _, updated, text, arts = parse_thread(page)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[{n}/{len(items)}] не удалось: {name}: {e}")
            continue
        new = to_site_articles(arts)
        old = d["a"]
        if not new or (len(old) >= 5 and len(new) < len(old) * MIN_KEEP):
            suspicious.append((name, f"было статей: {len(old)}, найдено: {len(new)}. Возможно, изменилась вёрстка форума или тема закрыта"))
            print(f"[{n}/{len(items)}] ПОДОЗРИТЕЛЬНО: {name}")
            continue
        added, removed, changed = compare(old, new)
        if not (added or removed or changed):
            unchanged += 1
            print(f"[{n}/{len(items)}] без изменений: {name}")
            continue
        print(f"[{n}/{len(items)}] ИЗМЕНЕНО: {name}: +{len(added)} -{len(removed)} ~{len(changed)}")
        changes.append({"id": it["id"], "name": name, "url": it["url"], "added": added, "removed": removed, "changed": changed})
        if args.apply:
            d["a"] = new
            nd = human_date(updated or "")
            if nd:
                d["u"] = nd
            applied.append(name)

    if bf:
        bf.close()

    # ---------- отчёт ----------
    L = ["# Отчёт бота: проверка законов на форуме", "",
         f"Проверено тем: {len(items)}. Без изменений: {unchanged}. С изменениями: {len(changes)}. "
         f"Не скачались: {len(failed)}. Подозрительные: {len(suspicious)}.", ""]
    for c in changes:
        L += [f"## {c['name']}", f"[Тема на форуме]({c['url']})", ""]
        for a in c["added"]:
            L += [f"- ➕ **Новая: {label(a)}.** {a['x'][:400]}"]
        for a in c["removed"]:
            L += [f"- ➖ **Убрана: {label(a)}.** {a['x'][:300]}"]
        for o, nw in c["changed"]:
            L += [f"- ✏️ **Изменена: {label(nw)}.** {word_diff(o['x'], nw['x'])}"]
        L.append("")
    if suspicious:
        L += ["## ⚠️ Проверьте вручную (бот эти темы НЕ менял)", ""] + [f"- {a}: {b}" for a, b in suspicious] + [""]
    if failed:
        L += ["## Не удалось скачать", ""] + [f"- {a}: {b}" for a, b in failed] + [""]
    if not changes and not suspicious and not failed:
        L += ["Изменений нет. Всё совпадает с форумом."]
    Path(args.report).write_text("\n".join(L), encoding="utf-8")

    # ---------- применение ----------
    new_ver = None
    if args.apply and applied:
        body = dump(data)
        new_ver = datetime.now(timezone.utc).strftime("%d.%m.%Y") + "-b" + hashlib.sha1(body.encode()).hexdigest()[:6]
        out = html[:s0] + body + html[s1:]
        out, k = re.subn(r'BASE_VER="[^"]*"', f'BASE_VER="{new_ver}"', out, count=1)
        if k != 1:
            sys.exit("Не нашёл BASE_VER в index.html. Файл не изменён.")
        Path(args.site).write_text(out, encoding="utf-8")
        print(f"index.html обновлён, версия базы {new_ver}")

    summary = {"changed": bool(applied) if args.apply else bool(changes), "found": len(changes),
               "suspicious": len(suspicious), "failed": len(failed), "version": new_ver}
    print(json.dumps(summary, ensure_ascii=False))
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            for k, v in summary.items():
                f.write(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n")
    # если форум недоступен почти целиком, завершаемся с ошибкой: GitHub пришлёт письмо
    if len(failed) >= max(3, len(items) // 2):
        print("Форум недоступен для бота (скорее всего, блокирует запросы).")
        sys.exit(2)


if __name__ == "__main__":
    main()
