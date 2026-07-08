# 雪茄上新/补货监测器

这个小工具会定时检查指定网站的商品，发现新品、重新出现的商品、价格变化时发通知。对 `cn.cohcigars.com` 这类能显示库存数量的网站，也会在库存数量增加时提醒。它只做监测和提醒，不会自动购买。

## 快速开始

1. 复制配置文件：

   ```powershell
   Copy-Item config.example.json config.json
   ```

2. 编辑 `config.json`：

   - `targets` 里已经放了 iHavanas 和 COH Cigars CN 的古巴品牌页
   - `check_interval_seconds` 建议 180 到 600 秒之间
   - 选择一种通知方式，把对应的 `enabled` 改成 `true`

3. 第一次运行先建立基准，不发提醒：

   ```powershell
   python monitor.py --init-state
   ```

4. 测试检查一次：

   ```powershell
   python monitor.py --once
   ```

5. 长期运行：

   ```powershell
   python monitor.py
   ```

## 推荐通知方式

- Telegram：实时、稳定，适合长期用。
- Bark：iPhone 用户很方便。
- PushPlus / Server 酱：国内网络环境通常更顺。
- 邮件：最朴素，也最容易排查。

## 增加第二个网站

在 `config.json` 的 `targets` 数组里复制现有配置块，改成类似这样：

```json
{
  "name": "Second shop - Cuban cigars",
  "url": "https://example.com/cuban-cigars",
  "urls": ["https://example.com/cuban-cigars"],
  "enabled": true,
  "include_keywords": ["Cohiba", "Montecristo", "Partagas"],
  "exclude_keywords": ["accessories", "humidor"],
  "product_url_contains": ["/cohiba-", "/montecristo-", "/partagas-"],
  "notify_on_new": true,
  "notify_on_restock": true,
  "notify_on_price_change": true
}
```

## Windows 定时运行

如果不想一直开着命令窗口，可以用“任务计划程序”每 5 分钟运行一次：

- 程序：`python`
- 参数：`monitor.py --once`
- 起始位置：这个文件夹的完整路径

更简单的办法是直接运行 `python monitor.py`，让窗口常驻。

## 电脑关机也监测

已经准备好 GitHub Actions 云端定时任务：`.github/workflows/cigar-monitor.yml`。

需要做三件事：

1. 把整个文件夹上传到 GitHub 仓库。
2. 在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 里新增密钥：

   ```text
   Name: SMTP_PASSWORD
   Value: 你的邮箱密码或应用专用密码
   ```

3. 打开仓库的 `Actions`，启用工作流。之后它会大约每 10 分钟检查一次。

云端任务发现新品、补货、库存增加或价格变化时，会发邮件到配置里的邮箱，并在邮件里列出商品名、价格、库存和链接。

## 注意

- 请确认购买、进口和持有相关商品符合你所在地法律法规。
- 不建议把检查频率设得太高，3 到 10 分钟一次比较稳。
- 有些网站会动态加载库存状态，如果某个网站抓不到，需要换成浏览器版监测器。
