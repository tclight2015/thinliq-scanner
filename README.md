# 薄流動性動能掃描儀

Binance 公開 API，無需 Key，支援現貨 + 永續雙市場。

## 本地測試

```bash
pip install -r requirements.txt
python app.py
# 開啟 http://localhost:5000
```

## 部署到 Render

1. 將整個資料夾推到 GitHub repo
2. 前往 https://render.com → New → Web Service
3. 連結你的 GitHub repo
4. 設定：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT`
   - **Plan**: Free
5. Deploy

完成後即可使用，無需任何 API Key。

## 功能說明

### 掃描器
- 點「掃描」後約 30-60 秒出結果（Render free tier 速度）
- 自動每 30 秒背景更新
- 支援篩選：資金規模、成交量倍數、市場、市值、最低評分

### 持倉追蹤
- 切換到「持倉追蹤」頁籤
- 輸入幣種 + 進場價格即可追蹤
- 顯示即時浮動盈虧、動態出場建議
- 每 60 秒自動刷新
- 持倉資料存在瀏覽器，重開頁面不會消失

## 指標說明

| 指標 | 說明 |
|------|------|
| 衝擊估算 | 你的資金進場能推動多少% |
| 成交量倍數 | 當前量 vs 過去 24 小時均量 |
| 主動買入比 | 近 200 筆成交中買方主動比例 |
| 資金費率 | 正 = 多頭付費（空頭友好），負 = 空頭付費 |
| 多空比 | >2 = 多頭過擁擠，<0.7 = 空頭過擁擠 |
| 信號有效期 | 依觸發指標估算窗口長度 |
