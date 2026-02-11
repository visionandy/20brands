# 20brands

多品牌电商 deal/sale 列表页爬虫，支持约 20 个品牌站点的促销商品抓取。

## 项目结构

```
20brands/
├── config/
│   └── sites_draft.json    # 站点配置（URL、选择器）
├── crawl_test.py          # 主爬虫脚本（Selenium）
├── google_search_product.py
├── output/                 # 抓取结果 JSON
└── README.md
```

## 环境要求

- Python 3.9+
- Chrome 浏览器
- Chromedriver（与 Chrome 版本匹配）
- `selenium`

```bash
pip install selenium
```

## 使用方法

按 `site_id` 爬取单个站点：

```bash
python crawl_test.py site_01   # Michael Kors
python crawl_test.py site_02   # Calvin Klein
python crawl_test.py site_05   # Alo Yoga
python crawl_test.py site_06   # J.Crew
python crawl_test.py site_07   # Tommy Hilfiger
python crawl_test.py site_08   # Nike
python crawl_test.py site_09   # Adidas
python crawl_test.py site_10   # Levi's
```

输出写入 `output/<品牌名>_test.json`。

## 配置说明

`config/sites_draft.json` 中每个站点包含：

| 字段 | 说明 |
|------|------|
| `list_url` | deal 列表页 URL |
| `selectors` | CSS 选择器：container、title、current_price、original_price、deal_url、image_url、deal_percent、deals_detail |
| `fetch_detail` | 是否抓取详情页 |
| `detail_selectors` | 详情页额外字段 |

## 输出字段

- `title` - 商品名称
- `current_price` - 现价
- `original_price` - 原价
- `deal_url` - 商品链接
- `image_url` - 主图 URL
- `deal_percent` / `deals_detail` - 折扣信息（可选）

## 已支持站点

| site_id | 品牌 |
|---------|------|
| site_01 | Michael Kors |
| site_02 | Calvin Klein |
| site_03 | Coach |
| site_04 | Lululemon |
| site_05 | Alo Yoga |
| site_06 | J.Crew |
| site_07 | Tommy Hilfiger |
| site_08 | Nike |
| site_09 | Adidas |
| site_10 | Levi's |

## 注意事项

- 爬虫模拟人类行为（分步滚动、随机延时、逐卡 scrollIntoView）
- 部分站点可能需处理 cookie 弹窗或页面加载超时
- 建议使用 `caffeinate -i` 防止息屏导致 session 失效
