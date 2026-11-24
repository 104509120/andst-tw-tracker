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


def extract_listed_date(html: str) -> str | None:
    """從 91APP 網頁原始碼提取商品上架開賣日期 (YYYY-MM-DD)"""
    date_patterns = [
        r'"SellingStartDateTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"StartDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"CreateDateTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'"OnlineDate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
    ]
    candidates = []
    for pat in date_patterns:
        for m in re.finditer(pat, html):
            d = m.group(1)
            # 過濾系統預設無效年份 (如 1900-01-01 或未來預約日期)
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

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Product"), {})
            if data.get("@type") == "Product":
                title = data.get("name", "").strip()
                imgs = data.get("image", [])
                image = imgs[0] if isinstance(imgs, list) and imgs else (imgs if isinstance(imgs, str) else "")
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

    listed_date = extract_listed_date(html) or TODAY

    return (
        "ok",
        {
            "sku": sku,
            "salepage_id": salepage_id,
            "url": url,
            "title": title,
            "image": image,
            "price": price,
            "original_price": original_price,
            "listed_date": listed_date,
        },
    )


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
        if sku not in by_sku or item["price"] < by_sku[sku]["price"]:
            by_sku[sku] = item

    index_list = []
    for sku, item in by_sku.items():
        item_path = f"data/items/{sku}.json"
        jp_search_url = f"https://www.dot-st.com/disp/CSfDispListPage_001.jsp?q={sku}"
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
        record["jp_url"] = jp_search_url
        record["salepage_id"] = item["salepage_id"]
        record["url"] = item["url"]
        record["title"] = item["title"]
        record["image"] = item["image"]
        record["current_price"] = item["price"]
        record["original_price"] = item["original_price"]

        history = record.get("history", [])

        # 若抓到的商品上架日早於目前最早紀錄，補上上架日當天以原始定價開賣的起點
        if listed_date < TODAY:
            if not history:
                history.append({"date": listed_date, "price": item["original_price"]})
            elif listed_date < history[0]["date"]:
                history.insert(0, {"date": listed_date, "price": item["original_price"]})

        # 寫入今日最新價格（若與上一筆價格不同才新增轉折點）
        if not history or history[-1]["price"] != item["price"]:
            if history and history[-1]["date"] == TODAY:
                history[-1]["price"] = item["price"]
            else:
                history.append({"date": TODAY, "price": item["price"]})

        prices = [h["price"] for h in history]
        record["min_price"] = min(prices)
        record["max_price"] = max(prices)
        record["history"] = history

        with open(item_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        index_list.append({
            "sku": sku,
            "salepage_id": item["salepage_id"],
            "listed_date": listed_date,
            "title": item["title"],
            "image": item["image"],
            "price": item["price"],
            "original_price": item["original_price"],
            "min_price": record["min_price"],
            "max_price": record["max_price"],
        })

    index_list.sort(key=lambda x: x["listed_date"], reverse=True)
    with open("data/index.json", "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": TODAY, "count": len(index_list), "items": index_list},
            f,
            ensure_ascii=False,
        )
    print(f"同步完成！共收錄 {len(index_list)} 筆不重複原廠貨號。", flush=True)


if __name__ == "__main__":
    main()
