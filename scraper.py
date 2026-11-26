import concurrent.futures
import gzip
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from curl_cffi import requests
from bs4 import BeautifulSoup

SITEMAP_INDEX_URL = "https://www.dot-st.tw/Sitemap/36426/Sitemap_Index.xml"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.dot-st.tw/",
}
TW_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TW_TZ).strftime("%Y-%m-%d")

# 本站「真正開始逐日記錄價格」的第一天。
# 早於此日的歷史資料並不是實際觀測到的價格（舊版程式曾以「上架日＋定價」補點），
# 一律視為無效，避免走勢圖出現不存在的調價日期。
TRACKING_START = "2026-11-23"

TW_ITEM_URL = "https://www.dot-st.tw/SalePage/Index/{salepage_id}"

# and ST TW 常見品牌對照表（依匹配精確度排序）
BRAND_PATTERNS = [
    ("apart by lowrys", re.compile(r"apart\s*by(?:\s*lowrys)?|a\s*part\s*by", re.IGNORECASE)),
    ("niko and ...", re.compile(r"niko\s*and(?:\s*[\.…]+)?", re.IGNORECASE)),
    ("LOWRYS FARM", re.compile(r"lowrys\s*farm", re.IGNORECASE)),
    ("GLOBAL WORK", re.compile(r"global\s*work", re.IGNORECASE)),
    ("studio CLIP", re.compile(r"studio\s*clip", re.IGNORECASE)),
    ("repipi armario", re.compile(r"repipi\s*armario", re.IGNORECASE)),
    ("mysty woman", re.compile(r"mysty\s*woman", re.IGNORECASE)),
    ("FOREVER 21", re.compile(r"forever\s*21", re.IGNORECASE)),
    ("BARNYARDSTORM", re.compile(r"barnyardstorm", re.IGNORECASE)),
    ("Curensology", re.compile(r"curensology", re.IGNORECASE)),
    ("JEANASIS", re.compile(r"jeanasis", re.IGNORECASE)),
    ("RAGEBLUE", re.compile(r"rageblue", re.IGNORECASE)),
    ("BABYLONE", re.compile(r"babylone", re.IGNORECASE)),
    ("BAYFLOW", re.compile(r"bayflow", re.IGNORECASE)),
    ("PAGEBOY", re.compile(r"pageboy", re.IGNORECASE)),
    ("Heather", re.compile(r"\bheather\b", re.IGNORECASE)),
    ("Andemiu", re.compile(r"andemiu", re.IGNORECASE)),
    ("LEPSIM", re.compile(r"lepsim", re.IGNORECASE)),
    ("LAKOLE", re.compile(r"lakole", re.IGNORECASE)),
    ("Elura", re.compile(r"\belura\b", re.IGNORECASE)),
    ("ALAND", re.compile(r"\baland\b", re.IGNORECASE)),
    ("Chaos", re.compile(r"\bchaos\b", re.IGNORECASE)),
    ("HARE", re.compile(r"\bhare\b", re.IGNORECASE)),
    ("Me%", re.compile(r"me%", re.IGNORECASE)),
    ("e/rm", re.compile(r"\be/rm\b", re.IGNORECASE)),
]

NON_BRAND_BRACKET_KEYWORDS = (
    "預購", "現貨", "特價", "限定", "任選", "免運", "滿額", "優惠",
    "新品", "熱銷", "推薦", "出清", "組合", "折", "SALE", "NEW", "HOT",
)


def fetch_url(url: str, timeout: int = 20, retries: int = 3):
    """使用模擬 Chrome 的 TLS 指紋發送請求，遇限流自動退避重試"""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url,
                headers=HEADERS,
                impersonate="chrome",
                timeout=timeout,
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


def decode_xml_bytes(raw: bytes) -> str:
    """自動處理 Gzip 壓縮檔 (.xml.gz) 與 UTF-8 BOM"""
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", errors="replace")


def get_all_salepage_urls() -> dict[str, str]:
    """從 Sitemap 取得全站上架中的 SalePageId 與網址"""
    print(f"正在讀取 Sitemap Index: {SITEMAP_INDEX_URL}", flush=True)
    resp = fetch_url(SITEMAP_INDEX_URL, timeout=25)
    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "無回應"
        print(f"[錯誤] 無法讀取 Sitemap Index，狀態：{code}", flush=True)
        sys.exit(1)

    index_text = decode_xml_bytes(resp.content)
    all_locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", index_text, flags=re.IGNORECASE)
    sub_sitemaps = [loc for loc in all_locs if "salepage" in loc.lower()]
    print(f"找到 {len(sub_sitemaps)} 個商品子 Sitemap：{sub_sitemaps}", flush=True)

    products = {}
    for sm_url in sub_sitemaps:
        sm_resp = fetch_url(sm_url, timeout=30)
        if not sm_resp or sm_resp.status_code != 200:
            print(f"[警告] 無法讀取子 Sitemap: {sm_url}", flush=True)
            continue

        sm_text = decode_xml_bytes(sm_resp.content)
        urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", sm_text, flags=re.IGNORECASE)
        for url in urls:
            match = re.search(r"/SalePage/Index/(\d+)", url)
            if match:
                products[match.group(1)] = url

    return products


def extract_sku(html: str, salepage_id: str) -> str | None:
    """多策略提取台日共用原廠貨號（通常為 6~7 碼數字）"""
    label_pattern = (
        r"(?:貨號|商品編號|款號|料號|品號|原廠編號|ITEM\s*NO\.?|STYLE\s*NO\.?)"
        r"(?:</[^>]+>|\s|<[^>]+>|[：:【】\[\]])*?(\d{6,7})"
    )
    match = re.search(label_pattern, html, flags=re.IGNORECASE)
    if match and match.group(1) != salepage_id:
        return match.group(1)

    outer_match = re.search(r'"OuterId"\s*:\s*"(\d{6,7})\d*"', html)
    if outer_match and outer_match.group(1) != salepage_id:
        return outer_match.group(1)

    return None


def match_known_brand(text: str) -> str | None:
    """比對已知品牌名稱，回傳標準化品牌名"""
    if not text:
        return None
    for canonical, pattern in BRAND_PATTERNS:
        if pattern.search(text):
            return canonical
    return None


def extract_brand(html: str, title: str, ld_brand: str = "") -> str:
    """從商品標題、JSON-LD 與頁面原始碼提取商品品牌"""
    # 1. 優先從商品標題匹配已知品牌（準確度最高）
    found = match_known_brand(title)
    if found:
        return found

    # 2. 檢查 JSON-LD brand 欄位（排除電商平台統稱）
    if ld_brand:
        found = match_known_brand(ld_brand)
        if found:
            return found

    # 3. 檢查網頁內的品牌規格或分類名稱
    spec_match = re.search(
        r"(?:品牌|Brand)(?:</[^>]+>|\s|<[^>]+>|[：:])+?([A-Za-z0-9\s\.\-%/]{2,30})",
        html,
        flags=re.IGNORECASE,
    )
    if spec_match:
        found = match_known_brand(spec_match.group(1))
        if found:
            return found

    # 4. 最後嘗試解析標題前綴括號（如【品牌名】），排除活動促銷標籤
    bracket_match = re.match(r"^\s*[【\[]([^】\]]{2,24})[】\]]", title)
    if bracket_match:
        candidate = bracket_match.group(1).strip()
        upper_c = candidate.upper()
        if not any(kw in upper_c for kw in NON_BRAND_BRACKET_KEYWORDS):
            return candidate

    return "其他品牌"


def extract_listed_date(html: str, ld_date: str = "") -> str | None:
    """從 91APP 網頁原始碼提取商品上架開賣日期 (YYYY-MM-DD)"""
    date_patterns = [
        r'"SellingStartDateTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"SellingStartTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"PublishDateTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"StartDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"CreateDateTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"OnlineDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    ]
    candidates = []
    if ld_date:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", ld_date.strip())
        if m and "2020-01-01" <= m.group(1) <= TODAY:
            candidates.append(m.group(1))

    for pat in date_patterns:
        for m in re.finditer(pat, html):
            d = m.group(1)
            if "2020-01-01" <= d <= TODAY:
                candidates.append(d)
    return min(candidates) if candidates else None


def parse_product_page(salepage_id: str, url: str) -> tuple[str, dict | str]:
    """解析單一商品頁，回傳狀態與結果"""
    time.sleep(0.6 + random.uniform(0.1, 0.4))
    resp = fetch_url(url, timeout=18, retries=2)
    if not resp or resp.status_code != 200:
        return ("http_err", url)

    html = resp.text
    sku = extract_sku(html, salepage_id)
    if not sku:
        return ("no_sku", url)

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    image = ""
    price = None
    original_price = None
    ld_brand = ""
    ld_date = ""

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Product"), {})
            if data.get("@type") == "Product":
                title = data.get("name", "").strip()
                imgs = data.get("image", [])
                image = imgs[0] if isinstance(imgs, list) and imgs else (imgs if isinstance(imgs, str) else "")
                raw_brand = data.get("brand", "")
                if isinstance(raw_brand, dict):
                    ld_brand = str(raw_brand.get("name", "")).strip()
                elif isinstance(raw_brand, str):
                    ld_brand = raw_brand.strip()
                ld_date = str(data.get("datePublished", "") or data.get("releaseDate", ""))
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                raw_price = offers.get("price") or offers.get("lowPrice")
                if raw_price:
                    price = int(float(raw_price))
                break
        except Exception:
            continue

    if not title:
        og_title = soup.find("meta", property="og:title")
        title = og_title["content"].split("|")[0].strip() if og_title else f"商品 {sku}"
    if not image:
        og_img = soup.find("meta", property="og:image")
        image = og_img["content"] if og_img else ""

    price_match = re.search(r'"Price"\s*:\s*(\d+)', html)
    suggest_match = re.search(r'"SuggestPrice"\s*:\s*(\d+)', html)
    if price is None and price_match:
        price = int(price_match.group(1))
    if suggest_match:
        original_price = int(suggest_match.group(1))
    if not original_price or (price and original_price < price):
        original_price = price

    if price is None:
        return ("no_price", url)

    listed_date = extract_listed_date(html, ld_date) or TODAY
    brand = extract_brand(html, title, ld_brand)

    return (
        "ok",
        {
            "sku": sku,
            "salepage_id": salepage_id,
            "brand": brand,
            "url": url,
            "title": title,
            "image": image,
            "price": price,
            "original_price": original_price,
            "listed_date": listed_date,
        },
    )


def sku_rank(item: dict):
    """同一貨號有多個 SalePage 時的挑選順序：標題含完整貨號者優先，其次取最低價"""
    return (0 if item["sku"] in item.get("title", "") else 1, item["price"])


def clean_history(record: dict) -> list[dict]:
    """
    只保留「真正逐日觀測到」的價格紀錄：
    - 丟棄早於 TRACKING_START 的紀錄（舊版以上架日補的假資料）
    - 同日去重、依日期排序、連續相同價格只留第一筆（僅記錄價格轉折點）
    """
    dedup = {}
    for h in record.get("history", []):
        date, price = h.get("date"), h.get("price")
        if date and price is not None and str(date)[:10] >= TRACKING_START:
            dedup[str(date)[:10]] = int(price)

    cleaned = []
    for d, p in sorted(dedup.items()):
        if cleaned and cleaned[-1]["price"] == p:
            continue
        cleaned.append({"date": d, "price": p})
    return cleaned


def last_price_drop(history: list[dict]) -> dict | None:
    """
    從價格轉折點找出「最近一次降價」：價格比前一筆低的最新一筆。
    只看本站實際觀測到的紀錄；回傳 {"date", "from", "to", "amount", "pct"}，從未降價則回傳 None。
    """
    for i in range(len(history) - 1, 0, -1):
        prev, cur = history[i - 1]["price"], history[i]["price"]
        if cur < prev:
            return {
                "date": history[i]["date"],
                "from": prev,
                "to": cur,
                "amount": prev - cur,
                "pct": round((prev - cur) / prev * 100, 1) if prev else 0,
            }
    return None


def main():
    os.makedirs("data/items", exist_ok=True)
    salepages = get_all_salepage_urls()
    total = len(salepages)
    print(f"從 Sitemap 成功取得 {total} 件商品網址，開始同步價格...", flush=True)

    if total == 0:
        print("[錯誤] 未取得任何商品網址，終止執行。", flush=True)
        sys.exit(1)

    results = []
    stats = {"ok": 0, "http_err": 0, "no_sku": 0, "no_price": 0}
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(parse_product_page, sp_id, url)
            for sp_id, url in salepages.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            done += 1
            status, payload = future.result()
            stats[status] += 1
            if status == "ok":
                results.append(payload)

            if done % 250 == 0 or done == total:
                print(
                    f"進度：{done}/{total} | 成功:{stats['ok']} 無貨號:{stats['no_sku']} 連線失敗:{stats['http_err']}",
                    flush=True,
                )

    by_sku = {}
    for item in results:
        sku = item["sku"]
        if sku not in by_sku or sku_rank(item) < sku_rank(by_sku[sku]):
            by_sku[sku] = item

    index_list = []
    for sku, item in by_sku.items():
        item_path = f"data/items/{sku}.json"
        if os.path.exists(item_path):
            with open(item_path, "r", encoding="utf-8") as f:
                record = json.load(f)
        else:
            record = {
                "sku": sku,
                "history": [],
            }

        listed_date = min(item["listed_date"], record.get("listed_date", item["listed_date"]))
        record["listed_date"] = listed_date
        record["updated_at"] = TODAY
        record["brand"] = item["brand"]
        record["salepage_id"] = item["salepage_id"]
        # 台灣官網商品頁：一律以 SalePageId 組出標準網址
        # 例：https://www.dot-st.tw/SalePage/Index/11726297
        record["url"] = TW_ITEM_URL.format(salepage_id=item["salepage_id"])
        record["title"] = item["title"]
        record["image"] = item["image"]
        record["current_price"] = item["price"]
        record["original_price"] = item["original_price"]
        # 日本官網網址由 jp_enrich.py 實際比對後寫入 record["jp"]["url"]；
        # 舊版固定寫入的搜尋連結不再保留
        record.pop("jp_url", None)

        # 只保留真實觀測到的價格紀錄（同時清掉舊版以上架日補的假資料）
        history = clean_history(record)

        # 寫入今日最新價格（僅記錄轉折點；若同日重複執行則覆蓋今日價格）
        if not history:
            history.append({"date": TODAY, "price": item["price"]})
        elif history[-1]["date"] == TODAY:
            history[-1]["price"] = item["price"]
            if len(history) >= 2 and history[-1]["price"] == history[-2]["price"]:
                history.pop()
        elif history[-1]["price"] != item["price"]:
            history.append({"date": TODAY, "price": item["price"]})

        # 本站第一次記錄到這件商品的日期（早於此日的調價日期無從得知）
        record["first_seen"] = min(history[0]["date"], record.get("first_seen") or history[0]["date"])
        if record["first_seen"] < TRACKING_START:
            record["first_seen"] = history[0]["date"]

        # 歷史低價 = 實際觀測到的最低價；歷史高價 = 觀測最高價與官方定價取大者
        observed = [h["price"] for h in history] + [item["price"]]
        record["min_price"] = min(observed)
        record["max_price"] = max(observed + [item["original_price"]])
        record["history"] = history
        drop = last_price_drop(history)
        record["last_drop"] = drop

        with open(item_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        index_list.append({
            "sku": sku,
            "salepage_id": item["salepage_id"],
            "brand": item["brand"],
            "listed_date": listed_date,
            "title": item["title"],
            "image": item["image"],
            "price": item["price"],
            "original_price": item["original_price"],
            "min_price": record["min_price"],
            "max_price": record["max_price"],
            # 最近一次降價（供「降價由新至舊」排序）；從未降價則為 null
            "last_drop_date": drop["date"] if drop else None,
            "last_drop_amount": drop["amount"] if drop else None,
            "last_drop_pct": drop["pct"] if drop else None,
        })

    # 預設依上架日期由新至舊排序（同日再依 91APP SalePageId 由新至舊）
    index_list.sort(key=lambda x: (x["listed_date"], int(x["salepage_id"])), reverse=True)
    with open("data/index.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": TODAY,
                "tracking_started": TRACKING_START,
                "count": len(index_list),
                "items": index_list,
            },
            f,
            ensure_ascii=False,
        )
    print(f"同步完成！共收錄 {len(index_list)} 筆不重複原廠貨號。", flush=True)


if __name__ == "__main__":
    main()
