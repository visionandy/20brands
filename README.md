# 20brands

多品牌电商 deal/sale 列表页爬虫，支持约 20 个品牌站点的促销商品抓取。

## 项目结构

```
20brands/
├── config/
│   ├── sites_draft.json    # 站点配置（URL、选择器）
│   └── html_samples/       # 各站点 HTML 样本
├── crawl_test.py           # 主爬虫脚本（Selenium）
├── process_deals_for_upload.py  # 数据处理：转 mysmartshop 格式 + GPT 分类
├── fetch_html_samples.py   # 抓取 HTML 样本
├── google_search_product.py
├── output/                 # 抓取结果（按日期归档，对齐 deal_crawler）
│   └── YYYY_MM_DD/         # 如 2026_02_22
│       ├── *_test.json     # 各站点 deal JSON
│       └── processed/      # 处理后输出（供 mysmartshop 导入）
├── requirements.txt
└── README.md
```

## 环境要求

- Python 3.9+
- Chrome 浏览器
- Chromedriver（与 Chrome 版本匹配）
- `selenium`
- `beautifulsoup4`（可选，用于 Athleta / Michael Kors / Guess / COS 的 HTML 兜底解析）

```bash
pip install -r requirements.txt
# 或: pip install selenium beautifulsoup4 openai python-dotenv
```

## 使用方法

按站点 id 爬取单个站点：

```bash
python crawl_test.py michaelkors   # Michael Kors
python crawl_test.py calvinklein   # Calvin Klein
python crawl_test.py athleta       # Athleta
python crawl_test.py guess         # Guess
python crawl_test.py cos           # COS
# ... 其他站点见下方列表
```

输出写入 `output/<YYYY_MM_DD>/<品牌名>_test.json`（按日期归档，对齐 deal_crawler）。

批量爬取所有站点（并行、headless、超时跳过）：

```bash
python crawl_test.py --all --workers 4 --headless
```

- `--all`：爬取配置中所有站点
- `--workers N`：并行线程数（默认 4）
- `--headless`：无头模式
- `--limit N`：每个站点仅爬前 N 条（测试用）
- 页面 40 秒未加载完成则自动跳过该站点

## 数据处理与上传 mysmartshop.net

将抓取结果转为 mysmartshop Product 格式，并用 GPT 做类别分类：

```bash
# 处理当日目录（默认 output/2026_02_22/）
python process_deals_for_upload.py --gpt-rewrite --merge all_products.json

# 指定日期目录
python process_deals_for_upload.py --date-dir 2026_02_22 --gpt-rewrite --merge all_products.json

# 处理单个文件
python process_deals_for_upload.py output/2026_02_22/Athleta_test.json
```

需在项目根目录或 `ecom/ecom` 或 `deal_crawler` 下配置 `.env`，内容 `OPENAI_API_KEY=sk-xxx`。

导入到 mysmartshop DB（在 ecom 项目下执行）：

```bash
cd ../ecom/ecom
python manage.py import_20brands_products ../../20brands/output/processed/all_products.json
```

查看：https://mysmartshop.net/admin/store/product/

## 自动读取 HTML 与解析

### 1. 抓取 HTML 样本

`fetch_html_samples.py` 自动打开列表页、滚动触发懒加载、保存完整 HTML 到 `config/html_samples/<site_id>.html`：

```bash
python fetch_html_samples.py guess    # 抓取单个站点
python fetch_html_samples.py          # 抓取所有站点
```

站点可配置 `fetch_html` 控制行为：
- `wait_for_selector`：等待该元素出现后再保存
- `scroll_before_save`：是否先滚动再保存（默认 true）
- `dismiss_consent`：是否尝试关闭 cookie 弹窗（默认 true）

### 2. 自动解析 HTML 兜底

`crawl_test.py` 在爬取时自动读取 `driver.page_source`（当前页 HTML），对部分站点用 BeautifulSoup 做兜底解析：

| 站点 | 解析方式 | 选择器（代码内固定） |
|------|----------|----------------------|
| Athleta | BeautifulSoup | `[data-testid='plp_product-card']`、`.plp-product-price-small`、`span.fds__core-web-original-price` |
| Michael Kors | BeautifulSoup | `article.product-tile-container`、`.price .sales .value`、`.price .list .value` |
| Guess | BeautifulSoup | `li.product__tile`、`.item-price`、`.price__strike-through.item-salesPrice .value` |
| COS | JSON-LD 正则 | `ItemList` 中的 `itemListElement`、`offers.lowPrice` / `highPrice` |

当 Selenium 主选择器未取到数据时，兜底逻辑会从 HTML 补全 title、价格等。

### 3. Guess 调试 HTML

Guess 站点爬取结束后会自动保存 `config/html_samples/guess_debug.html`，供人工检查 Algolia 渲染后的 DOM 结构，便于更新选择器。

## 配置说明

`config/sites_draft.json` 中每个站点包含：

| 字段 | 说明 |
|------|------|
| `id` | 站点唯一标识 |
| `list_url` | deal 列表页 URL |
| `selectors` | CSS 选择器：container、title、current_price、original_price、deal_url、image_url、deal_percent、deals_detail |
| `fetch_detail` | 是否抓取详情页 |
| `detail_selectors` | 详情页额外字段（输出为 detail_<key>） |

全局配置 `global` 可包含：
- `original_price_fallback_selectors`：原价 selector 为空时的备选选择器
- `title_clean`：title 清洗规则

## 输出格式

每个 deal 包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_site` | string | 站点 id |
| `title` | string | 商品名称 |
| `current_price` | float \| null | 现价（数值，用于排序/计算） |
| `current_price_display` | string | 现价展示串（如 `"$24.97"`、`"MYR 599.20"`） |
| `original_price` | float \| null | 原价（数值） |
| `original_price_display` | string | 原价展示串 |
| `deal_url` | string | 商品链接 |
| `image_url` | string | 主图 URL |
| `deal_percent` | string | 折扣百分比（可选） |
| `deals_detail` | string | 折扣详情（可选） |

价格解析逻辑：
- 支持多币种：`$`、`€`、`£`、`RM`、`MYR`、`USD`、`SGD` 等
- 区间价格（如 `$43.98 - $69.98`）取第一个数字
- 无法解析时：`current_price` / `original_price` 为 `null`，`*_display` 为 `""`

## 站点兜底逻辑

部分站点在 Selenium 中可能未完全渲染，会使用 BeautifulSoup 从 `page_source` 兜底补全：

| 站点 | 兜底方式 |
|------|----------|
| Athleta | HTML 解析 `[data-testid='plp_product-card']` |
| Michael Kors | HTML 解析 `article.product-tile-container` |
| Guess | HTML 解析 `li.product__tile`（Algolia 未渲染时） |
| COS | JSON-LD `ItemList` 解析（补全 title、image、价格） |

## 已支持站点

| id | 品牌 |
|---------|------|
| michaelkors | Michael Kors |
| calvinklein | Calvin Klein |
| coach | Coach |
| lululemon_athletica | Lululemon |
| alo_yoga | Alo Yoga |
| j_crew | J.Crew |
| tommy_hilfiger | Tommy Hilfiger |
| nike | Nike |
| adidas | Adidas |
| levis | Levi's |
| h_m | H&M |
| target | Target |
| columbia | Columbia |
| athleta | Athleta |
| guess | Guess |
| tory_burch | Tory Burch |
| tory_sport | Tory Sport |
| theory | Theory |
| cos | COS |

## 注意事项

- 爬虫模拟人类行为：分步滚动、随机延时、逐卡 scrollIntoView、Stale 元素跳过
- 部分站点可能需处理 cookie 弹窗或页面加载超时
- 建议使用 `caffeinate -i` 防止息屏导致 session 失效
- 站点页面结构可能变更，导致 selector 失效（如 Adidas 等需定期校验）
