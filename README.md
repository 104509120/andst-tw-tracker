# andst-tw-tracker
# And st TW 商品歷史價格追蹤

每天自動抓取 [and ST 台灣官網](https://www.dot-st.tw/) 的商品價格，記錄每次調價，並附上日本官網的商品說明與使用心得（翻成繁體中文）。

## 價格資料怎麼來

- `scraper.py`：每天台灣時間 12:00 由 GitHub Actions 執行，從 Sitemap 讀取全站商品並更新 `data/`。
- **本站從 2026-11-23 起才開始逐日記錄價格。** 走勢圖上的實線與圓點都是「實際偵測到」的價格與日期；
  上架日到開始追蹤日之間的調價日期無法取得，圖上以灰色虛線表示，不會假裝有調價。
- 調價日以「本站第一次偵測到新價格的日期」為準（每天同步一次）。

## 日本官網資料（商品說明 / 使用心得）

- `jp_enrich.py`（`.github/workflows/jp_enrich.yml`）：每天台灣時間 04:00 執行，與價格爬蟲分開，失敗不影響價格紀錄。
- 做的事：比對日本官網的商品頁網址（例：`https://www.dot-st.com/pageboy/disp/item/635139/`）、
  擷取「商品詳細」與「レビューの要約」、翻成台灣繁體中文，寫入 `data/items/{貨號}.json` 的 `jp` 欄位。
- 每次處理最多 1500 件（新上架優先），所以全站補齊需要幾天。

### 翻譯設定（建議）

在 repo 的 **Settings → Secrets and variables → Actions** 新增 Secret：

| 名稱 | 說明 |
| --- | --- |
| `ANTHROPIC_API_KEY` | 使用 Claude 翻譯，語氣最自然。沒有設定時會退回免金鑰的 Google 翻譯端點（可用，但較生硬）。 |

### 手動測試單一商品

Actions → **Enrich JP product info** → **Run workflow**，填入貨號（例：`635139`）並勾選 `debug`。
執行完成後可在該次執行頁面下載 `jp-debug`（日本官網原始 HTML），方便檢查解析結果。
