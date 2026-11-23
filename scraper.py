import concurrent.futures
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

SITEMAP_INDEX_URL = "https://www.dot-st.tw/Sitemap/36426/Sitemap_Index.xml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
TW_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TW_TZ).strftime("%Y-%m-%d")


def get_all_salepage_urls() -> dict[str, str]:
    """從 Sitemap 取得全站上架中的 SalePageId 與網址"""
    resp = requests.get(SITEMAP_INDEX_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    sub_sitemaps = [
        loc.text.strip()
        for loc in root.findall(".//{*}loc")
        if loc.text and "salepage" in loc.text.lower()
    ]

    products = {}
    for sm_url in sub_sitemaps:
        sm_resp = requests.get(sm_url, headers=HEADERS, timeout=20)
        sm_resp.raise_for_status()
        sm_root = ET.fromstring(sm_resp.content)
        for loc in sm_root.findall(".//{*}loc"):
            if not loc.text:
                continue
            url = loc.text.strip()
            match = re.search(r"/SalePage/Index/(\d+)", url)
            if match:
                products[match.group(1)] = url
    return products


def parse_product_page(salepage_id: str, url: str) -> dict | None:
    """解析單一商品頁：抓取 6~7 碼原廠貨號、名稱、售價、原價與圖片"""
    try:
        time.sleep(0.3)  # 避免請求過快
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text

        # 1. 提取原廠貨號 (對應 <td><h2>貨號</h2></td><td><h2>659900</h2></td>)
        sku_match = re.search(
            r"貨號(?:</[^>]+>|\s|<[^>]+>|[：:])*?(\d{5,8})", html
        )
        if not sku_match:
            return None
        sku = sku_match.group(1)

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        image = ""
        price = None
        original_price = None

        # 2. 從 ld+json 讀取標準商品資訊
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

        # 3. 補齊原價與備用欄位
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
            return None

        return {
            "sku": sku,
            "salepage_id": salepage_id,
            "url": url,
            "title": title,
            "image": image,
            "price": price,
            "original_price": original_price,
        }
    except Exception:
        return None


def main():
    os.makedirs("data/items", exist_ok=True)
    salepages = get_all_salepage_urls()
    print(f"從 Sitemap 取得 {len(salepages)} 件商品，開始同步價格...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(parse_product_page, sp_id, url)
            for sp_id, url in salepages.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            if item:
                results.append(item)

    # 依原廠貨號 (SKU) 彙整（避免同款多色或重新上架造成歷史紀錄分散）
    by_sku = {}
    for item in results:
        sku = item["sku"]
        if sku not in by_sku or item["price"] < by_sku[sku]["price"]:
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
                "jp_url": f"https://www.and-st.jp/search?q={sku}",
                "history": [],
            }

        record["salepage_id"] = item["salepage_id"]
        record["url"] = item["url"]
        record["title"] = item["title"]
        record["image"] = item["image"]
        record["current_price"] = item["price"]
        record["original_price"] = item["original_price"]

        history = record.get("history", [])
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
            "title": item["title"],
            "image": item["image"],
            "price": item["price"],
            "original_price": item["original_price"],
            "min_price": record["min_price"],
            "max_price": record["max_price"],
        })

    index_list.sort(key=lambda x: x["sku"])
    with open("data/index.json", "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": TODAY, "count": len(index_list), "items": index_list},
            f,
            ensure_ascii=False,
        )
    print(f"同步完成！共收錄 {len(index_list)} 筆不重複原廠貨號。")


if __name__ == "__main__":
    main()
