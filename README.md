# buyip18-scanner — iPhone 18 Pro Max 秒級庫存掃描器

單 GitHub runner 內部循環最長 ~5.5 小時：mihomo 載入訂閱節點（secret）秒切出口，
每節點 ≥15 秒一次請求（聚合 ≈ 每 1-2 秒掃全 8 SKU），見貨 → dispatch
buyip18-checkout（SKU+店精準打擊）+ Bark → job 結束；cron */15 watchdog 自動續命。

與 5-8 分一拍的雷達並存：兩者都可 trigger 下單通道，checkout 的
concurrency（cancel-in-progress）令最新派工自動接管。
