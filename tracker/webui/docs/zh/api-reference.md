# API 参考

API 第 1 版的全部接口。所有接口都是 `GET`，以 JSON 返回，地址都在 `{{api}}` 之下。除前两个接口外，其余都需要密钥，参见 [API 入门](api)。

## 概览

| 接口 | 返回内容 |
| --- | --- |
| `GET /v1` | 这里有哪些接口，以及文档在哪里。无需密钥。 |
| `GET /v1/health` | API 是否在运行，以及追踪系统的版本。无需密钥。 |
| `GET /v1/me` | 你正在使用的密钥，以及它代表的账户。 |
| `GET /v1/projects` | 园区列表，可筛选、排序和分页。 |
| `GET /v1/projects/{id}` | 单个园区的完整信息，每个字段都附有来源。 |
| `GET /v1/projects/{id}/sources` | 单个园区背后的全部引用。 |
| `GET /v1/companies/{slug}` | 单个公司的园区和近期新闻。 |
| `GET /v1/capex` | 按买方统计的容量和投资。 |
| `GET /v1/updates` | 你关注列表中的近期新闻。 |

## GET /v1/projects

园区列表，与 [Projects](projects)（项目）表格中显示的一致，筛选条件也相同。

| 参数 | 含义 |
| --- | --- |
| `q` | 自由文本。每个词都必须出现在园区的名称、公司、租户、城市、县、州或最大障碍中。`#42` 表示 42 号园区。 |
| `state` | 两个字母的州代码，例如 `TX`。 |
| `phase` | `announced`、`permitting`、`construction`、`operational`、`paused` 或 `cancelled`。 |
| `min_confidence` | `1`、`2` 或 `3`。参见[可信度](confidence)。 |
| `obstacle` | 存在这一类未解决障碍的园区，例如 `grid_capacity`。 |
| `severity` | 存在至少达到这一严重程度的未解决障碍的园区：`watch`、`material`、`blocking`。 |
| `quoted_only` | 为 `true` 时，只保留没有任何待确认、推断或默认值的园区。 |
| `suspected_delay` | 为 `true` 时，只保留疑似延期的园区（见下文）。 |
| `sort` | 排序字段：`confidence`（默认）、`mw_planned`、`investment_usd`、`expected_online`、`updated_at`、`id`，或任何其他追踪字段。`days_late` 按疑似延期的逾期天数排序，配合 `desc` 时逾期最久的排在最前，其他园区排在它们之后。 |
| `order` | `desc`（默认）或 `asc`。空值总是排在最后。 |
| `limit` | 每页行数，1–200。默认 30。 |
| `offset` | 跳过的行数。默认 0。 |

未知的参数或未知的排序字段会返回 `400`，而不是被忽略。

```json
{
  "total": 118,
  "offset": 0,
  "limit": 30,
  "next_offset": 30,
  "data": [
    {
      "id": 42,
      "name": "North Campus",
      "company": "Acme Data Centers",
      "customer": null,
      "city": "Abilene",
      "county": "Taylor County",
      "state": "TX",
      "phase": "construction",
      "mw_planned": 1200.0,
      "mw_built": null,
      "investment_usd": 15000000000,
      "first_announced": "2025-03-01",
      "expected_online": "2027-01-01",
      "blocker": "The utility has not yet approved the substation upgrade.",
      "h200_equivalent": 920000,
      "confidence": 2,
      "lat": 32.45,
      "lon": -99.73,
      "updated_at": "2026-10-14T12:31:07",
      "suspected_delay": null,
      "links": {"self": "/v1/projects/42"}
    }
  ]
}
```

以上数值均为虚构，仅作示例。

### suspected_delay

除非该园区属于疑似延期，否则为 `null`。疑似延期是指：园区自己的预计上线日期已过 —— 是该日期所指的整个时段都已结束 —— 之后也没有任何报道表明它已投入运行。规则与控制台使用的相同，完整说明见[数字与日期](numbers-and-dates)。属于疑似延期时，它是一个对象：

| 键 | 含义 |
| --- | --- |
| `expected_online` | 存储的日期，例如 `2025-07-01`。 |
| `precision` | 来源记录的精度：`month`、`quarter`、`half`、`year`；精确到日或没有记录时为 `null`。 |
| `read_as` | 规则理解该日期时采用的精度：有记录时即 `precision`；否则为 `day`，每月 1 日为 `month`，1 月 1 日为 `year`。 |
| `stated` | 读者习惯的写法：`Q3 2025`、`H2 2026`、`2026`、`Jul 2026`、`15 Jul 2026`。 |
| `due` | 该时段的最后一天。园区从第二天起算逾期。 |
| `days_late` | 从 `due` 到 `as_of` 的天数。 |
| `as_of` | 计算这一结果的日期。它每天都在变，所以 `days_late` 只有和它放在一起才有意义。 |
| `reason` | 用一句平实的话说明原因。 |

```json
"suspected_delay": {
  "expected_online": "2025-07-01",
  "precision": "quarter",
  "read_as": "quarter",
  "stated": "Q3 2025",
  "due": "2025-09-30",
  "days_late": 385,
  "as_of": "2026-10-20",
  "reason": "Expected online Q3 2025, a quarter that ended 385 days ago; the phase is still construction and nothing reported since says it is running."
}
```

## GET /v1/projects/{id}

单个园区：包含摘要中的全部内容，另外还有：

| 键 | 含义 |
| --- | --- |
| `fields` | 每个追踪字段，形式为 `{value, label, quote, quote_is_exact, source_url}`；在有意义时还附带 `scope`、`bound`、`modality`、`as_of`、`basis` 或 `date_precision`。`label` 取值为 `quoted`、`derived`、`unconfirmed`、`inferred`、`default`、`missing` 或 `not_applicable`。 |
| `parties` | 涉及的公司：`name`、`role`（`developer` 开发商、`owner` 业主、`operator` 运营商、`customer` 客户、`utility` 公用事业公司、`contractor` 承包商）、`quote` 和 `confirmed`。 |
| `tranches` | 楼栋和分期：`label`、`status`、`mw`、`mw_counted`（该容量是否计入园区总数）、`customer`、`expected_online`、`energized_on`、`investment_usd`。 |
| `obstacles` | `category`、`severity`、`status`、`summary`、`quote`、`first_seen`、`resolved_at`。 |
| `milestones` | `date`、`type`、`description`、`quote` 和 `confirmed`。 |
| `source_count` | 引用的数量；`links.sources` 列出这些引用。 |

```json
{
  "data": {
    "id": 42,
    "name": "North Campus",
    "fields": {
      "mw_planned": {
        "value": 1200.0,
        "label": "quoted",
        "quote": "The campus will draw 1.2 GW at full build-out.",
        "quote_is_exact": true,
        "source_url": "https://news.example.com/north-campus",
        "basis": "facility"
      }
    }
  }
}
```

`quote_is_exact` 为 `false` 表示这段文字是来源的摘录，而不是为该字段记录的原句。已被合并进另一个园区的园区会返回 `404`，并说明它可能已被合并。

## GET /v1/projects/{id}/sources

每条引用：`url`、`type`（`company_filing` 公司文件、`government_doc` 政府文件、`trade_press` 行业媒体、`general_media` 综合媒体、`iso_queue` 电网并网队列或 `manual` 人工录入）、`published_at`、`fetched_at`、`excerpt`，以及 `quotes`，即该来源所支持的每个字段记录下的原句。

## GET /v1/companies/{slug}

以数据形式提供的公司页面：`name`、`website`、`totals`（园区数，分别按运营商和租户身份统计，规划和运行的兆瓦数，未解决的障碍）、`states`、`phases`、它的 `projects`（每个都附有其角色、阶段、容量、障碍和 `suspected_delay`）、最近两个月报道的 `updates`，以及 `suspected_delays` —— 疑似延期园区的数量 `count`、它们合计的规划容量 `mw_planned`，以及按逾期从久到短排列的 `project_ids`。slug 是公司名称的小写连字符形式，与控制台中该公司页面地址里的一样：`compass-datacenters`。

## GET /v1/capex

以数据形式提供的 [Capex](capex)（资本支出）。`buyers` 中每个买方一行，包括 `(no named customer)`：

| 键 | 含义 |
| --- | --- |
| `customer`、`projects` | 买方，以及园区数量。 |
| `self_built`、`undisclosed` | 根据所有权归属的园区，以及没有点名的租户。 |
| `mw_planned`、`mw_built` | 规划和运行的兆瓦数。 |
| `investment_usd` | 已确认的投资。`investment_unquoted_usd`、`investment_excluded_usd` 和 `investment_out_of_scope_usd` 是已报道但处理方式不同的金额，各自单独披露。 |
| `mw_by_year` | 按年份统计的预计投运兆瓦数。 |
| `mw_at_risk`、`projects_at_risk`、`slipped` | 存在未解决障碍的园区的容量、这类园区的数量，以及其中有多少已经延期。 |
| `duplicate_rows_skipped`、`mw_duplicate_skipped` | 疑似重复、被单独搁置而不重复计算的行。 |
| `project_ids` | 构成这一行的园区。 |

每个数字都是下限，暂停和已取消的园区不计入。

## GET /v1/updates

你关注列表近期被报道的内容，与你的 [Updates](updates)（动态）页面相同。

| 参数 | 含义 |
| --- | --- |
| `days` | 回溯天数，1–60。默认 7。 |

`data` 包含已确认的动态，最重要的排在最前；`unconfirmed` 包含无人能引证的动态。每条动态都有 `kind`、`sign`（`good` 利好、`bad` 利空、`neutral` 中性）、`project_id`、`company`、`project`、`headline`、`detail`、`happened`、`reported`、`expected`、`quote`、`source_url` 和 `publisher`。`last_crawl` 说明上次抓取文章的时间。关注列表为空时，`data` 也为空。

## GET /v1/me

密钥（`name`、`prefix`、`created_at`、`last_used_at`）及其账户（`email`、`name`）。绝不返回密钥本身。
