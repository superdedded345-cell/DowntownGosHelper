#!/usr/bin/env python3
"""
Скачивает все документы законодательной базы сервера с docossum.com,
режет их на статьи и сохраняет в JSON / JSONL для последующего поиска и ИИ.

Примеры:
    python scrape_docossum.py                      # все документы Downtown
    python scrape_docossum.py --refresh            # перекачать (проверить обновления)
    python scrape_docossum.py --only 1801714       # только Конституция
    python scrape_docossum.py --server downtown --delay 3

Результат (папка data/<server>/):
    documents.json   - документы со статьями (для сайта: вкладка «Законы»)
    chunks.jsonl     - плоский список фрагментов (для поиска / эмбеддингов / ИИ)
    raw/<id>.html    - кэш скачанных страниц
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

BASE = "https://docossum.com"
UA = "Mozilla/5.0 (compatible; LawAssistantBot/1.0; personal project)"
MAX_CHARS = 2500          # максимум символов в одном фрагменте
FALLBACK_CHARS = 1200     # размер фрагмента для документов без «Статья N.»

BLOCK_TAGS = ["p", "div", "li", "tr", "td", "th", "table", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"]

ARTICLE_RE = re.compile(r"^Статья\s+(\d+(?:\.\d+)*)\s*[.:)–—-]?\s*(.*)$")
CHAPTER_RE = re.compile(r"^(Глава|Раздел|Часть)\s+[IVXLCDM\d]+[.\s:–—-].{0,140}$", re.I)
NUMBERED_HEAD_RE = re.compile(r"^\d+\.\s+[^\n]{2,100}$")
DECOR_RE = re.compile(r"^[▬▭■□◆◇●○•\-_=—–~*.\s]{4,}$")
TOC_ITEM_RE = re.compile(r"Глава\s+[IVXLCDM\d]+\.")
UPDATED_RE = re.compile(r"Обновлен\s+(\d{2}\.\d{2}\.\d{4}(?:,\s*\d{2}:\d{2}:\d{2})?)")


# ───────────────────────── сеть ─────────────────────────

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    return s


def get(session, url, retries=3, delay=2.0):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(delay * attempt)
    raise RuntimeError(f"Не удалось скачать {url}: {last}")


def check_robots(session, url):
    """Уважаем robots.txt. Если файла нет или он недоступен — разрешаем."""
    rp = robotparser.RobotFileParser()
    try:
        r = session.get(BASE + "/robots.txt", timeout=15)
        if r.status_code != 200:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA, url)
    except requests.RequestException:
        return True


# ───────────────────────── разбор страниц ─────────────────────────

def list_documents(html, server):
    """Достаёт из страницы список документов (id, название, ссылка)."""
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(rf"/legal/{re.escape(server)}/document/(\d+)/?$")
    docs, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        docs.append({
            "id": m.group(1),
            "title": a.get_text(" ", strip=True),
            "url": f"{BASE}/legal/{server}/document/{m.group(1)}",
        })
    return docs


def page_title(soup):
    if soup.title and soup.title.get_text(strip=True):
        return re.split(r"\s+[—–-]\s+", soup.title.get_text(strip=True))[0].strip()
    return ""


def detect_doc_id(soup, server):
    """ID документа по <link rel=canonical> / og:url; запасной вариант — по названию."""
    pat = re.compile(rf"/legal/{re.escape(server)}/document/(\d+)")
    cands = []
    for l in soup.find_all("link", href=True):
        if "canonical" in (l.get("rel") or []):
            cands.append(l["href"])
    for m in soup.find_all("meta", attrs={"property": "og:url"}):
        cands.append(m.get("content", ""))
    for c in cands:
        m = pat.search(c)
        if m:
            return m.group(1)
    # запасной вариант: сопоставить заголовок страницы со ссылками меню
    title = page_title(soup)
    if title:
        for a in soup.find_all("a", href=True):
            m = pat.search(a["href"])
            if m and a.get_text(" ", strip=True) == title:
                return m.group(1)
    return None


def scan_saved(folders, server):
    """Офлайн: ищет в папках любые *.html/*.htm (имена не важны) и определяет документ по содержимому.
    Возвращает (документы, нераспознанные файлы, дубликаты, все известные документы {id: название})."""
    pat = re.compile(rf"/legal/{re.escape(server)}/document/(\d+)/?$")
    found, skipped, dups, known = {}, [], [], {}
    files = []
    for folder in folders:
        for ext in ("*.html", "*.htm"):
            files.extend(Path(folder).rglob(ext))
    for f in sorted(set(files)):
        if f.name.startswith("_"):
            continue
        try:
            soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        except Exception:
            skipped.append(f)
            continue
        for a in soup.find_all("a", href=True):      # меню со списком всех документов
            m = pat.search(a["href"])
            if m and a.get_text(strip=True):
                known.setdefault(m.group(1), a.get_text(" ", strip=True))
        doc_id = detect_doc_id(soup, server)
        if not doc_id:
            skipped.append(f)
            continue
        title = page_title(soup) or doc_id
        prev = found.get(doc_id)
        if prev is not None:
            dups.append((doc_id, title, prev["path"].name, f.name))
        if prev is None or f.stat().st_mtime > prev["mtime"]:
            found[doc_id] = {"id": doc_id, "title": title, "path": f,
                             "mtime": f.stat().st_mtime,
                             "url": f"{BASE}/legal/{server}/document/{doc_id}"}
    docs = sorted(found.values(), key=lambda d: int(d["id"]))
    for d in docs:
        d.pop("mtime")
    return docs, skipped, dups, known


def clean_text(s):
    s = s.replace("\u200b", "").replace("\xa0", " ")
    out = []
    for line in s.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    return "\n".join(out).strip()


def extract_text(html):
    """Возвращает (чистый текст документа, ссылка на оригинал, дата обновления)."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()

    original = None
    for a in soup.find_all("a", href=True):
        if "оригинал" in a.get_text().lower():
            original = a["href"]
            break

    # убираем навигацию: якоря глав, меню документов, историю изменений
    doc_link = re.compile(r"/legal/[^/]+/document/\d+/?(history/?)?$")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith("#") or doc_link.search(h) or h.rstrip("/").endswith("/history"):
            a.decompose()

    # переносы строк: <br> и блочные теги
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.append("\n")

    text = clean_text(soup.get_text(""))

    # начало контента — после кнопок «Создать законопроект»
    marker = "Создать законопроект"
    if marker in text:
        text = text.split(marker, 1)[1]
    # конец контента — строка «Обновлен dd.mm.yyyy, hh:mm:ss»
    updated = None
    m = UPDATED_RE.search(text)
    if m:
        updated = m.group(1)
        text = text[:m.start()]
    return clean_text(text), original, updated


DOTTED_RE = re.compile(r"(?m)^(\d+(?:\.\d+)+)(?:\s+(?=\S)|(?=\[))")


def resplit_dotted(articles):
    """Большие куски без «Статья N» (в УАК пункты вида «12.6 Мелкое хулиганство ... Штраф»)
    режем по строкам с номером «N.M» — это и есть статьи кодекса."""
    out = []
    for a in articles:
        if a["number"] or len(DOTTED_RE.findall(a["text"])) < 3:
            out.append(a)
            continue
        parts = DOTTED_RE.split(a["text"])
        if parts[0].strip():
            out.append({**a, "text": parts[0].strip()})
        for k in range(1, len(parts), 2):
            out.append({"chapter": a["chapter"], "number": parts[k], "title": "",
                        "text": parts[k + 1].strip()})
    return out


def split_into_articles(text):
    """Режет текст на статьи. Возвращает список словарей."""
    lines = text.splitlines()
    articles, chapter, current = [], "", None

    def flush():
        nonlocal current
        if current is not None:
            body = "\n".join(current["lines"]).strip()
            if body or current["title"]:
                articles.append({
                    "chapter": current["chapter"],
                    "number": current["number"],
                    "title": current["title"],
                    "text": body,
                })
        current = None

    for line in lines:
        if line and DECOR_RE.match(line):
            continue
        if not line:
            if current is not None:
                current["lines"].append("")
            continue
        if CHAPTER_RE.match(line) and not ARTICLE_RE.match(line):
            flush()
            chapter = line
            continue
        m = ARTICLE_RE.match(line)
        if m:
            flush()
            rest = m.group(2).strip()
            # короткий хвост считаем названием статьи, длинный — началом текста
            title, first = (rest, "") if 0 < len(rest) <= 150 and not rest.endswith(".") else ("", rest)
            current = {"chapter": chapter, "number": m.group(1), "title": title,
                       "lines": [first] if first else []}
            continue
        if current is None:
            current = {"chapter": chapter, "number": "", "title": "" if chapter else "Преамбула", "lines": []}
        current["lines"].append(line)
    flush()
    return resplit_dotted(articles)


def split_fallback(text):
    """Для документов без «Статья N.» (уставы и т.п.): режем по абзацам."""
    parts, head, buf, size = [], "", [], 0

    def flush():
        nonlocal buf, size
        if buf:
            parts.append({"chapter": "", "number": "", "title": head,
                          "text": "\n".join(buf).strip()})
        buf, size = [], 0

    for line in text.splitlines():
        if not line:
            continue
        if CHAPTER_RE.match(line) or (NUMBERED_HEAD_RE.match(line) and not line.endswith(".")):
            flush()
            head = line
            continue
        if size + len(line) > FALLBACK_CHARS:
            flush()
        buf.append(line)
        size += len(line) + 1
    flush()
    return parts


def drop_toc(articles):
    """Убирает склеенное оглавление («Глава I… Глава II… Глава III…») в начале документа."""
    out = []
    for i, a in enumerate(articles):
        if i < 3 and not a["number"] and len(a["text"]) < 1200 and len(TOC_ITEM_RE.findall(a["text"])) >= 3:
            continue
        out.append(a)
    return out


def parse_document(html):
    text, original, updated = extract_text(html)
    articles = drop_toc(split_into_articles(text))
    structured = [a for a in articles if a["number"]]
    if len(structured) < 3:
        articles = drop_toc(split_fallback(text))
    return {
        "source": original,
        "updated": updated,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "chars": len(text),
        "articles": articles,
    }


def make_chunks(server, doc):
    """Плоский список фрагментов для поиска/ИИ. Длинные статьи делим по абзацам."""
    chunks = []
    for i, art in enumerate(doc["articles"], 1):
        label = f"Статья {art['number']}" if art["number"] else (art["title"] or f"Раздел {i}")
        paras = art["text"].split("\n")
        pieces, cur = [], ""
        for p in paras:
            if cur and len(cur) + len(p) + 1 > MAX_CHARS:
                pieces.append(cur)
                cur = p
            else:
                cur = f"{cur}\n{p}" if cur else p
        if cur:
            pieces.append(cur)
        for j, piece in enumerate(pieces, 1):
            chunks.append({
                "id": f"{server}-{doc['id']}-{i:03d}-{j}",
                "server": server,
                "doc_id": doc["id"],
                "doc_title": doc["title"],
                "chapter": art["chapter"],
                "article": label,
                "article_title": art["title"],
                "ref": " / ".join(x for x in (doc["title"], art["chapter"], label + (". " + art["title"] if art["title"] and art["number"] else "")) if x),
                "text": piece.strip(),
                "url": doc["url"],
            })
    return chunks


# ───────────────────────── основной сценарий ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="downtown", help="код сервера в URL (по умолчанию downtown)")
    ap.add_argument("--out", default="data", help="папка для результата")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза между запросами, сек")
    ap.add_argument("--only", nargs="*", help="скачать только эти ID документов")
    ap.add_argument("--refresh", action="store_true", help="игнорировать кэш и перекачать")
    ap.add_argument("--input", nargs="*", default=[],
                    help="папки с сохранёнными HTML (любые имена файлов), для офлайн-режима")
    ap.add_argument("--offline", action="store_true",
                    help="не ходить в сеть: брать только сохранённые HTML-файлы (из raw/ и из --input)")
    args = ap.parse_args()

    out_dir = Path(args.out) / args.server
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    session = make_session()
    index_url = f"{BASE}/legal/{args.server}"
    offline = args.offline
    index_cache = raw_dir / "_index.html"
    docs = []

    if not offline:
        if not check_robots(session, index_url):
            sys.exit("robots.txt запрещает скачивание. Остановлено.")
        try:
            if index_cache.exists() and not args.refresh:
                index_html = index_cache.read_text(encoding="utf-8")
            else:
                index_html = get(session, index_url, delay=args.delay)
                index_cache.write_text(index_html, encoding="utf-8")
                time.sleep(args.delay)
            docs = list_documents(index_html, args.server)
        except RuntimeError as e:
            print(f"{e}\nПереключаюсь на офлайн-режим: беру сохранённые файлы из {raw_dir}")
            offline = True

    if offline or not docs:
        offline = True
        docs, skipped, dups, known = scan_saved([raw_dir, *args.input], args.server)
        have = {d["id"] for d in docs}
        print(f"Найдено HTML-файлов с документами: {len(docs)}")
        if dups:
            print("\nОдин и тот же документ сохранён несколько раз (взят более свежий):")
            for did, title, f1, f2 in dups:
                print(f"  {did} {title}: {f1}  <->  {f2}")
        if skipped:
            print(f"\nНе распознаны как документы ({len(skipped)}):")
            for f in skipped[:20]:
                print(f"  {f.name}")
        missing = [(i, t) for i, t in sorted(known.items(), key=lambda x: int(x[0])) if i not in have]
        if missing:
            print(f"\nНЕ ХВАТАЕТ документов ({len(missing)}): сохраните эти страницы")
            for i, t in missing:
                print(f"  {i}  {t}")
        print()

    if not docs:
        sys.exit(f"Нет документов. Положите сохранённые страницы в {raw_dir} или укажите папку: --offline --input ПАПКА")
    if args.only:
        docs = [d for d in docs if d["id"] in set(args.only)]
    print(f"Документов к обработке: {len(docs)}")

    prev = {}
    prev_path = out_dir / "documents.json"
    if prev_path.exists():
        for d in json.loads(prev_path.read_text(encoding="utf-8")):
            prev[d["id"]] = d.get("hash")

    result, all_chunks, problems = [], [], []
    for n, d in enumerate(docs, 1):
        cache = d.get("path") or (raw_dir / f"{d['id']}.html")
        try:
            if cache.exists() and (offline or not args.refresh):
                html = cache.read_text(encoding="utf-8", errors="ignore")
            elif offline:
                raise RuntimeError("нет сохранённого файла")
            else:
                html = get(session, d["url"], delay=args.delay)
                cache.write_text(html, encoding="utf-8")
                time.sleep(args.delay)
            parsed = parse_document(html)
        except Exception as e:  # один сбойный документ не должен ронять всё
            problems.append((d["id"], d["title"], str(e)))
            print(f"[{n}/{len(docs)}] ОШИБКА {d['title']}: {e}")
            continue

        doc = {**{k: v for k, v in d.items() if k != 'path'}, **parsed}
        status = "новый" if d["id"] not in prev else ("ИЗМЕНЁН" if prev[d["id"]] != doc["hash"] else "без изменений")
        print(f"[{n}/{len(docs)}] {d['title'][:55]:55} статей/частей: {len(doc['articles']):4}  {status}")
        if not doc["articles"]:
            problems.append((d["id"], d["title"], "текст не найден"))
        result.append(doc)
        all_chunks.extend(make_chunks(args.server, doc))

    if args.only and prev_path.exists():
        # частичный запуск: не затираем остальные документы
        old = {d["id"]: d for d in json.loads(prev_path.read_text(encoding="utf-8"))}
        old.update({d["id"]: d for d in result})
        result = list(old.values())
        all_chunks = []
        for d in result:
            all_chunks.extend(make_chunks(args.server, d))

    prev_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"\nГотово: {len(result)} документов, {len(all_chunks)} фрагментов -> {out_dir}")
    if problems:
        print("\nПроверьте вручную:")
        for pid, title, err in problems:
            print(f"  {pid}  {title}: {err}")


if __name__ == "__main__":
    main()
