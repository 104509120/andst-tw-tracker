#!/usr/bin/env python3
"""
日本 and ST 官網資料補強（獨立於每日價格爬蟲，失敗不影響價格紀錄）

對每件商品：
  1. 找出日本官網「確實存在」的商品頁網址（例：https://www.dot-st.com/pageboy/disp/item/635139/）
  2. 擷取頁面上的「商品詳細」與「レビューの要約」日文內容
  3. 翻成流暢的台灣繁體中文
  4. 寫入 data/items/{sku}.json 的 "jp" 欄位

用法：
  python jp_enrich.py                       # 依優先順序補強，預設每次最多 1500 件
  python jp_enrich.py --limit 300           # 自訂處理件數
  python jp_enrich.py --sku 635139 --debug  # 只測單一商品，並把原始 HTML 存到 debug/jp/

翻譯：
  - 若有環境變數 ANTHROPIC_API_KEY，使用 Claude（品質最佳，建議）
  - 否則退回免金鑰的 Google 翻譯端點（可用，但語氣較生硬）
"""
import argparse
import concurrent.futures
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta

JP_BASE = "https://www.dot-st.com"
TW_TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TW_TZ)
TODAY = NOW.strftime("%Y-%m-%d")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://www.dot-st.com/",
}

# 已由官網實際網址確認的品牌代號。其餘品牌一律透過官網搜尋頁比對，不亂猜。
BRAND_SLUG_HINTS = {
    "PAGEBOY": "pageboy",
    "LEPSIM": "lepsim",
}

SECTION_DETAIL = "商品詳細"
SECTION_REVIEW = "レビューの要約"

# 出現這些獨立標題時，表示前一個區塊已經結束
STOP_TITLES = [
    "商品詳細", "レビューの要約", "レビュー", "お客様のレビュー", "カスタマーレビュー",
    "サイズ", "サイズ表", "サイズについて", "サイズ・スペック", "コーディネート",
    "この商品を見た人は", "この商品を買った人は", "関連商品", "おすすめ商品", "おすすめ",
    "最近チェックした商品", "ランキング", "配送・返品について", "お届けについて", "返品・交換について",
    "ショップ情報", "ブランド情報", "ブランドについて", "よくあるご質問", "よくある質問",
    "商品に関するお問い合わせ", "お問い合わせ",
]
NOISE_LINES = {"もっと見る", "閉じる", "続きを読む", "詳細を見る", "すべて見る", "▼", "▲", ">", "＞", "もっとみる"}

# 狀態重試間隔（天）
REFRESH_OK_DAYS = 30
RETRY_NOT_FOUND_DAYS = 7
RETRY_BLOCKED_DAYS = 1


# --------------------------------------------------------------------------- #
# 網路
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 25, retries: int = 2):
    from curl_cffi import requests  # 延後匯入，方便離線測試解析函式

    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, headers=HEADERS, impersonate="chrome", timeout=timeout, allow_redirects=True
            )
            if resp.status_code in (403, 429, 503) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return resp
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2 * (attempt + 1))
    return None


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


_STOP_NORMS = {_norm(t) for t in STOP_TITLES}


def _clean_lines(raw: str) -> str:
    lines = []
    for line in raw.replace("　", " ").split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        # 行尾黏著的「もっと見る」等按鈕文字
        line = re.sub(r"\s*(?:もっと見る|もっとみる|続きを読む|詳細を見る|すべて見る|閉じる)\s*$", "", line).strip()
        if not line or line in NOISE_LINES:
            continue
        lines.append(line)
    text = "\n".join(lines)
    return text[:4000].strip()


def _collect_after(node, own_title: str) -> str:
    """從標題節點之後依文件順序收集文字，直到遇到下一個區塊標題"""
    from bs4 import NavigableString, Comment

    block_tags = {
        "p", "div", "li", "ul", "ol", "br", "tr", "dt", "dd", "dl", "table",
        "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "th", "td", "button",
    }
    own = _norm(own_title)
    parts = []
    for el in node.next_elements:
        if isinstance(el, Comment):
            continue
        if isinstance(el, NavigableString):
            t = str(el).strip()
            if not t:
                continue
            n = _norm(t)
            if n != own and n in _STOP_NORMS:
                break
            parts.append(t)
        elif getattr(el, "name", None) in block_tags:
            parts.append("\n")
        if sum(len(p) for p in parts) > 6000:
            break
    return _clean_lines(" ".join(parts).replace(" \n ", "\n").replace("\n ", "\n").replace(" \n", "\n"))


def extract_section(soup, title: str) -> str:
    """找出標題為 title 的區塊文字；若頁面上有多處（如導覽頁籤），取內容最長者"""
    t_norm = _norm(title)
    best = ""
    for node in soup.find_all(string=True):
        n = _norm(str(node))
        if not n:
            continue
        if n == t_norm or (n.startswith(t_norm) and len(n) <= len(t_norm) + 6):
            text = _collect_after(node, title)
            # 若標題與內文同一段，去掉標題本身
            if len(text) > len(best):
                best = text
    return best


def jsonld_description(soup) -> str:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if isinstance(d, dict) and d.get("@type") == "Product" and d.get("description"):
                return _clean_lines(str(d["description"]).replace("\\n", "\n"))
    return ""


def parse_jp_page(html: str) -> dict:
    """回傳 {"detail": str, "review": str}（找不到則為空字串）"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    ld_desc = jsonld_description(soup)  # 先取，因為下面會移除 script
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    detail = extract_section(soup, SECTION_DETAIL)
    review = extract_section(soup, SECTION_REVIEW)

    detail_source = "page"
    if len(detail) < 15 and len(ld_desc) >= 15:
        detail, detail_source = ld_desc, "jsonld"
    if len(detail) < 15:
        detail = ""
    if len(review) < 10:
        review = ""
    return {"detail": detail, "review": review, "detail_source": detail_source}


# --------------------------------------------------------------------------- #
# 官網商品頁網址比對
# --------------------------------------------------------------------------- #
def _abs(url: str) -> str:
    return url if url.startswith("http") else JP_BASE + url


def resolve_jp_page(sku: str, brand: str, known_url: str | None):
    """
    回傳 (status, url, html)
    status: ok / not_found / blocked
    """
    candidates = []
    if known_url:
        candidates.append(known_url)
    slug = BRAND_SLUG_HINTS.get(brand)
    if slug:
        candidates.append(f"{JP_BASE}/{slug}/disp/item/{sku}/")

    blocked = False
    for url in dict.fromkeys(candidates):
        resp = fetch(url)
        if resp is not None and resp.status_code == 200 and sku in resp.text:
            return "ok", str(getattr(resp, "url", url)) or url, resp.text
        if resp is not None and resp.status_code in (403, 429):
            blocked = True

    # 從官網搜尋頁找出該貨號實際的商品連結（取得正確的品牌代號）
    resp = fetch(f"{JP_BASE}/disp/itemlist/?q={sku}")
    if resp is not None and resp.status_code == 200:
        m = re.search(
            rf'href=["\']((?:https?://(?:www\.)?dot-st\.com)?/[A-Za-z0-9_\-]+/disp/item/{re.escape(sku)}/?)["\']',
            resp.text,
        )
        if m:
            url = _abs(m.group(1))
            if not url.endswith("/"):
                url += "/"
            page = fetch(url)
            if page is not None and page.status_code == 200:
                return "ok", url, page.text
            if page is not None and page.status_code in (403, 429):
                blocked = True
    elif resp is not None and resp.status_code in (403, 429):
        blocked = True

    return ("blocked" if blocked else "not_found"), None, ""


# --------------------------------------------------------------------------- #
# 翻譯（日文 → 台灣繁體中文）
# --------------------------------------------------------------------------- #
SYSTEM_DETAIL = (
    "你是專業的日文轉台灣繁體中文服飾電商譯者。請把使用者提供的日本服飾商品說明，"
    "翻成流暢、自然、符合台灣用語習慣的繁體中文。規則：\n"
    "1. 只輸出譯文，不要加任何說明、前言或結語。\n"
    "2. 保留原文的換行與條列結構。\n"
    "3. 數字、尺寸、素材成分比例、洗滌方式必須照實翻譯，不可增減或臆測原文沒有的資訊。\n"
    "4. 品牌名、型號、色名可保留原文或採台灣常見譯名。\n"
    "5. 使用台灣慣用詞彙（例如：褲子、外套、洗衣機、尺寸、材質）。"
)
SYSTEM_REVIEW = (
    "你是專業的日文轉台灣繁體中文服飾電商譯者。以下是日本官網對顧客使用心得所做的摘要，"
    "請翻成流暢、自然、符合台灣用語習慣的繁體中文，語氣保持客觀。規則：\n"
    "1. 只輸出譯文，不要加任何說明、前言或結語。\n"
    "2. 保留原文的換行與條列結構。\n"
    "3. 不可增減或臆測原文沒有的資訊。\n"
    "4. 使用台灣慣用詞彙（例如：褲子、外套、尺寸、版型、質感）。"
)


def translate_claude(text: str, system: str) -> str | None:
    from curl_cffi import requests

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("JP_TRANSLATE_MODEL", "claude-haiku-5-5")
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 2000,
                    "system": system,
                    "messages": [{"role": "user", "content": text}],
                },
                timeout=60,
            )
            if resp.status_code == 200:
                blocks = resp.json().get("content", [])
                out = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                return out or None
            if resp.status_code in (429, 500, 502, 503, 529):
                time.sleep(4 * (attempt + 1))
                continue
            print(f"[翻譯] Claude API 回應 {resp.status_code}: {resp.text[:200]}", flush=True)
            return None
        except Exception as exc:
            print(f"[翻譯] Claude API 例外：{exc}", flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def translate_gtx(text: str) -> str | None:
    """免金鑰備援：Google 翻譯公開端點（品質較生硬）"""
    from curl_cffi import requests

    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1500 and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)

    out = []
    for chunk in chunks:
        ok = False
        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "ja", "tl": "zh-TW", "dt": "t"},
                    data={"q": chunk.strip("\n")},
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
                    ok = True
                    break
                time.sleep(3 * (attempt + 1))
            except Exception:
                time.sleep(3 * (attempt + 1))
        if not ok:
            return None
    return "\n".join(out).strip() or None


def translate(text: str, kind: str):
    """回傳 (譯文或 None, 引擎名稱)"""
    system = SYSTEM_DETAIL if kind == "detail" else SYSTEM_REVIEW
    zh = translate_claude(text, system)
    if zh:
        return zh, "claude"
    zh = translate_gtx(text)
    if zh:
        return zh, "gtx"
    return None, ""


# --------------------------------------------------------------------------- #
# 單件處理與排程
# --------------------------------------------------------------------------- #
def days_since(date_str: str | None) -> float:
    if not date_str:
        return 9999
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=TW_TZ)
        return (NOW - d).total_seconds() / 86400
    except Exception:
        return 9999


def priority(rec: dict) -> int | None:
    """數字越小越優先；None 表示目前不需處理"""
    jp = rec.get("jp") or {}
    status = jp.get("status")
    age = days_since(jp.get("checked_at"))
    if not status:
        return 0
    if status == "ok":
        needs_tr = (jp.get("detail_ja") and not jp.get("detail_zh")) or (
            jp.get("review_ja") and not jp.get("review_zh")
        )
        if needs_tr:
            return 1
        return 2 if age >= REFRESH_OK_DAYS else None
    if status == "blocked":
        return 3 if age >= RETRY_BLOCKED_DAYS else None
    return 3 if age >= RETRY_NOT_FOUND_DAYS else None


def process_item(rec: dict, debug: bool = False) -> dict:
    """回傳新的 jp 欄位內容"""
    sku = rec["sku"]
    old = rec.get("jp") or {}
    time.sleep(0.8 + random.uniform(0.1, 0.5))

    status, url, html = resolve_jp_page(sku, rec.get("brand", ""), old.get("url"))
    jp = dict(old)
    jp["checked_at"] = TODAY

    if status != "ok":
        jp["status"] = status
        return jp

    jp["url"] = url
    if debug:
        os.makedirs("debug/jp", exist_ok=True)
        with open(f"debug/jp/{sku}.html", "w", encoding="utf-8") as f:
            f.write(html)

    parsed = parse_jp_page(html)
    if not parsed["detail"] and not parsed["review"]:
        jp["status"] = "no_sections"
        return jp

    jp["status"] = "ok"
    for kind, key in (("detail", "detail"), ("review", "review")):
        ja = parsed[key]
        if not ja:
            jp.pop(f"{key}_ja", None)
            jp.pop(f"{key}_zh", None)
            continue
        if ja != old.get(f"{key}_ja") or not old.get(f"{key}_zh"):
            jp[f"{key}_ja"] = ja
            zh, engine = translate(ja, kind)
            if zh:
                jp[f"{key}_zh"] = zh
                jp["translator"] = engine
            else:
                jp.pop(f"{key}_zh", None)
    jp["detail_source"] = parsed.get("detail_source", "page")
    return jp


def load_record(sku: str) -> dict | None:
    path = f"data/items/{sku}.json"
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_record(rec: dict):
    with open(f"data/items/{rec['sku']}.json", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500, help="本次最多處理幾件")
    ap.add_argument("--budget-min", type=int, default=120, help="超過此分鐘數就不再開始新的項目")
    ap.add_argument("--sku", help="只處理指定貨號（測試用）")
    ap.add_argument("--debug", action="store_true", help="儲存原始 HTML 並印出解析結果")
    args = ap.parse_args()

    if not os.path.exists("data/index.json"):
        print("找不到 data/index.json，請先執行 scraper.py", flush=True)
        sys.exit(1)
    with open("data/index.json", "r", encoding="utf-8") as f:
        index_items = json.load(f).get("items", [])

    if args.sku:
        targets = [args.sku]
    else:
        # 只處理目前仍在架上的商品，新上架者優先
        ordered = sorted(index_items, key=lambda x: x.get("listed_date", ""), reverse=True)
        scored = []
        for it in ordered:
            rec = load_record(it["sku"])
            if not rec:
                continue
            p = priority(rec)
            if p is not None:
                scored.append((p, it["sku"]))
        scored.sort(key=lambda x: x[0])  # 穩定排序：同優先度維持新品在前
        targets = [sku for _, sku in scored][: args.limit]

    print(f"本次預計處理 {len(targets)} 件；翻譯引擎："
          f"{'Claude' if os.environ.get('ANTHROPIC_API_KEY') else 'Google 免金鑰備援'}", flush=True)

    deadline = time.time() + args.budget_min * 60
    stats = {"ok": 0, "no_sections": 0, "not_found": 0, "blocked": 0, "untranslated": 0}
    done = 0

    def work(sku: str):
        rec = load_record(sku)
        if not rec:
            return sku, None, None
        if time.time() > deadline:
            return sku, rec, None
        return sku, rec, process_item(rec, debug=args.debug)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for sku, rec, jp in ex.map(work, targets):
            done += 1
            if jp is None:
                continue
            rec["jp"] = jp
            save_record(rec)
            stats[jp["status"]] = stats.get(jp["status"], 0) + 1
            if jp["status"] == "ok" and (
                (jp.get("detail_ja") and not jp.get("detail_zh"))
                or (jp.get("review_ja") and not jp.get("review_zh"))
            ):
                stats["untranslated"] += 1
            if args.debug:
                print(json.dumps(jp, ensure_ascii=False, indent=2), flush=True)
            if done % 50 == 0 or done == len(targets):
                print(f"進度 {done}/{len(targets)} | {stats}", flush=True)

    print(f"完成：{stats}", flush=True)
    if stats["blocked"] and not stats["ok"]:
        print("[警告] 日本官網拒絕連線（可能封鎖資料中心 IP），本次沒有取得任何頁面。", flush=True)


if __name__ == "__main__":
    main()
