#!/usr/bin/env python3
"""
Скачивает законы прямо с форума forum.gta5rp.com (первоисточник) и режет на статьи.
Формат результата такой же, как у scrape_docossum.py, поэтому дальше всё работает одинаково.

Нужен файл sources.txt: по одной теме на строку, "ссылка<TAB>название" (название необязательно).

    python scrape_forum.py --limit 1                 # пробный запуск на одной теме
    python scrape_forum.py                           # все темы из sources.txt
    python scrape_forum.py --refresh                 # перекачать (проверка обновлений)
    python scrape_forum.py --offline --input папка   # без сети: разобрать сохранённые из браузера страницы

Результат: data/<server>-forum/documents.json и chunks.jsonl
Берётся первое сообщение темы и подряд идущие продолжения («Глава…», «Раздел…», «Статья…»).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

from scrape_docossum import (BLOCK_TAGS, clean_text, drop_toc,
                             get, make_chunks, make_session, split_fallback,
                             split_into_articles)

FORUM = "https://forum.gta5rp.com"
CONT_RE = re.compile(r"^\W*(Глава|Раздел|Часть|Статья|Приложение)\b", re.I)
JUNK_RE = re.compile(r"<span[^>]*>.*?</span>|&nbsp;|\u200b")
THREAD_ID_RE = re.compile(r"\.(\d+)/?(?:[#?].*)?$")


def thread_id(url):
    m = THREAD_ID_RE.search(url.strip())
    return m.group(1) if m else None


def index_saved(folders):
    """Офлайн: находит id темы внутри сохранённых страниц (имена файлов не важны)."""
    found = {}
    pats = [re.compile(r'rel=["\']canonical["\'][^>]*href=["\']([^"\']+)'),
            re.compile(r'href=["\']([^"\']+)["\'][^>]*rel=["\']canonical'),
            re.compile(r'property=["\']og:url["\'][^>]*content=["\']([^"\']+)'),
            re.compile(r"saved from url=\(\d+\)(\S+)")]
    for folder in folders:
        for f in Path(folder).rglob("*.htm*"):
            html = f.read_text(encoding="utf-8", errors="ignore")
            head = html[:20000]
            tid = None
            for pat in pats:
                m = pat.search(head)
                if m and thread_id(m.group(1)):
                    tid = thread_id(m.group(1))
                    break
            if tid:
                found[tid] = html
    return found


def post_text(node):
    for t in node(["script", "style", "noscript"]):
        t.decompose()
    for br in node.find_all("br"):
        br.replace_with("\n")
    for tag in node.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.append("\n")
    return clean_text(node.get_text(""))


def parse_thread(html):
    """Разбор страницы темы XenForo: заголовок, текст первого сообщения, дата правки."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("h1.p-title-value") or soup.find("h1")
    if h1:
        for lab in h1.select(".label, .labelLink"):      # приставки вроде «Важно», «Рассмотрено»
            lab.decompose()
        title = h1.get_text(" ", strip=True)

    posts = [p.select_one(".bbWrapper") for p in soup.select("article.message--post")]
    posts = [p for p in posts if p is not None] or soup.select(".bbWrapper")
    text = JUNK_RE.sub(" ", post_text(posts[0])) if posts else ""
    # длинный закон автор часто продолжает в следующих сообщениях: подряд идущие
    # сообщения, которые начинаются с «Глава / Раздел / Статья ...», добавляем к тексту
    for extra in posts[1:]:
        more = JUNK_RE.sub(" ", post_text(extra))
        if more and CONT_RE.match(more):
            text += "\n" + more
        else:
            break
    text = clean_text(text)

    if not text:                                          # запасной вариант: JSON-LD
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string or "")
            except ValueError:
                continue
            body = (data.get("mainEntity") or data).get("articleBody") if isinstance(data, dict) else None
            if body:
                text = clean_text(body)
                break

    updated = None
    edit = soup.select_one(".message-lastEdit time") or soup.select_one("article.message--post time")
    if edit:
        updated = edit.get("datetime") or edit.get_text(strip=True)

    articles = drop_toc(split_into_articles(text))
    if len([a for a in articles if a["number"]]) < 3:
        articles = drop_toc(split_fallback(text))
    return title, updated, text, articles


def load_sources(path):
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, title = line.partition("\t")
        tid = thread_id(url)
        if tid:
            items.append({"id": tid, "url": url.strip(), "title": title.strip()})
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--server", default="downtown")
    ap.add_argument("--out", default="data")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--limit", type=int, help="обработать только первые N тем (для пробы)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--input", nargs="*", default=[], help="папки с сохранёнными страницами (офлайн)")
    args = ap.parse_args()

    out_dir = Path(args.out) / f"{args.server}-forum"
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    items = load_sources(args.sources)
    if args.limit:
        items = items[:args.limit]
    if not items:
        sys.exit("В sources.txt нет ссылок на темы.")

    session = make_session()
    if not args.offline:
        try:                                              # уважаем robots.txt форума
            from urllib import robotparser
            rp = robotparser.RobotFileParser()
            r = session.get(FORUM + "/robots.txt", timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                if not all(rp.can_fetch("*", it["url"]) for it in items[:1]):
                    sys.exit("robots.txt форума запрещает скачивание. Остановлено.")
        except Exception:
            pass
    saved = index_saved([raw_dir, *map(Path, args.input)]) if args.offline else {}
    docs, chunks, problems = [], [], []
    for n, it in enumerate(items, 1):
        cache = raw_dir / f"{it['id']}.html"
        html = None
        try:
            if args.offline:
                html = saved.get(it["id"])
                if not html:
                    raise RuntimeError("нет сохранённой страницы")
            elif cache.exists() and not args.refresh:
                html = cache.read_text(encoding="utf-8")
            else:
                html = get(session, it["url"], delay=args.delay)
                cache.write_text(html, encoding="utf-8")
                time.sleep(args.delay)
            title, updated, text, articles = parse_thread(html)
        except Exception as e:
            problems.append((it["url"], str(e)))
            print(f"[{n}/{len(items)}] ОШИБКА {it['title'] or it['url']}: {e}")
            continue
        if not articles:
            problems.append((it["url"], "текст не найден (возможно, изменилась вёрстка или нужен вход)"))
        doc = {"id": it["id"], "title": it["title"] or title, "url": it["url"], "source": it["url"],
               "updated": updated, "chars": len(text), "articles": articles}
        print(f"[{n}/{len(items)}] {doc['title'][:55]:55} статей/частей: {len(articles):4}")
        docs.append(doc)
        chunks.extend(make_chunks(f"{args.server}-forum", doc))

    (out_dir / "documents.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\nГотово: {len(docs)} документов, {len(chunks)} фрагментов -> {out_dir}")
    if problems:
        print("\nПроверьте вручную:")
        for u, e in problems:
            print(f"  {u}: {e}")


if __name__ == "__main__":
    main()
