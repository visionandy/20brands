# Deal 爬虫配置草稿说明

## 一、文件夹结构（已创建）

```
20brands/
├── config/
│   ├── sites_draft.json    # 20 个站点 URL + 选择器配置草稿
│   └── CONFIG_DRAFT.md     # 本说明（配置含义与填写方式）
├── output/                  # 爬取结果输出目录（按需使用）
└── google_search_product.py # 原有脚本（后续可拆出 deal 爬虫逻辑）
```

## 二、配置草稿在做什么

- **sites_draft.json**：定义「爬哪 20 个站、每个站抓什么」。
  - **global**：全局参数（请求间隔、**每页停留时间**、超时、是否用 Selenium、结果输出目录）。
  - **deal_fields**：统一输出的 deal 字段名（title、价格、链接、来源站等）。
  - **sites**：长度为 20 的数组，每个元素对应一个站点：
    - **id**：站点唯一标识（如 site_01 … site_20）。
    - **name**：站点显示名（用于结果里的 source_site 或报表）。
    - **list_url**：该站的 deal 列表页 URL（爬虫只访问这个页面抓列表；若需分页再扩展）。
    - **notes**：备注（替换示例、待补充等）。
    - **selectors**：该站页面上的解析规则（见下）。

- **selectors** 里计划放的（按站填写）：
  - **container**：页面上「一个 deal 卡片」的父元素 CSS 选择器（用来先找出所有卡片，再在每个卡片内取下面字段）。
  - **title**：deal 标题（CSS 或 XPath）。
  - **current_price**：当前价/折扣价。
  - **original_price**：原价（没有就留空）。
  - **deal_url**：deal 详情或商品链接（一般是 `<a href>`）。
  - **image_url**：主图（可选，没有就留空）。

后续若某站需要「点击展开」「滚动加载」或分页，可以在该站点下增加 `pagination`、`scroll_to_load` 等配置，实现时再按配置读即可。

## 三、你这边需要填的内容

1. **把 20 个真实站点定下来**  
   在 `sites_draft.json` 里，把每个站点的 `name` 改成真实名称，`list_url` 改成该站 deal 列表页的真实 URL（可先只填第一页，分页以后再说）。

2. **为每个站填 selectors**  
   用浏览器打开该站的 deal 列表页，用开发者工具查看：
   - 哪个元素包住「一条 deal」（卡片/行）→ 填到 **container**。
   - 标题、价格、链接、图片分别对应哪个元素 → 填到 **title / current_price / original_price / deal_url / image_url**。  
   若某站结构复杂（例如价格在多个 span 里拼起来），可以在 **notes** 里写清楚，实现时针对该站写一小段解析逻辑或在该站下增加 `parser: "custom"` 之类标记，由代码里单独处理。

3. **global 按需改**  
   - `request_delay_sec`：**站与站之间**的随机延时（秒），例如 `[1, 3]` 表示每爬完一个站后等 1～3 秒再访问下一站。  
   - `page_dwell_sec`：**每个列表页打开后**的停留时间（秒），例如 `[8, 18]` 表示每页加载完成后随机等 8～18 秒再解析、再离开；停留长一点可让懒加载/JS 渲染完成，也更接近真人行为。  
   - `timeout_sec`、`use_selenium`、`output_dir` 按你环境与需求改即可。

## 四、输出格式（约定草稿）

- 爬虫跑完后，计划输出至少一种统一格式，例如：
  - **按站汇总**：`output/deals_by_site.json` → `{ "site_01": [ {...}, ... ], "site_02": [ ... ], ... }`。
  - **扁平列表**：`output/deals_all.json` → `[ { "title": "...", "current_price": "...", "deal_url": "...", "source_site": "site_01", ... }, ... ]`，每条带 `source_site` 标明来自哪个站。  
  具体文件名和是否拆成两个文件，实现时再定；这里先约定「每条 deal 都带 source_site、且字段与 deal_fields 一致」。

## 五、实现时打算怎么做（不写代码，只做流程）

1. **读配置**：加载 `config/sites_draft.json`，读 `global` 和 `sites`。
2. **按站循环**：对 `sites` 里每个站点，用 Selenium（或 requests，若该站 HTML 够用）访问 `list_url`；若有 consent/cookie 弹窗，先调类似现有的 dismiss 逻辑。
3. **每页停留**：页面加载完成后，按 `global.page_dwell_sec`（如 `[8, 18]`）随机等待若干秒再继续，以便懒加载/JS 渲染完成，并降低被识别为机器人的概率。
4. **按站解析**：停留结束后再取 `page_source` 或通过 Selenium 取元素；用该站 `selectors` 里的 container 找所有卡片，在每个卡片内用 title/current_price/... 选择器取文本或属性，拼成一条 deal 对象，补上 `source_site = 该站 id`。
5. **防封与稳健**：站与站之间用 `global.request_delay_sec` 随机延时；单站失败可重试 1～2 次再记失败并继续下一站。
6. **写结果**：把当前站或全部站的结果按上面「输出格式」写入 `output/` 目录；若某站解析失败，结果里可记 `site_xx: []` 或留一条错误信息便于排查。

这样，**文件夹和配置草稿**已经搭好，你只需要：补全 20 个站点的真实 `list_url` 和每个站的 `selectors`，之后实现代码就可以只依赖这一份配置来「爬 20 个网站的 deal 信息」。
