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
| `GET /v1/projects/{id}/facts` | 来源关于单个园区的每一条陈述，每条一行。 |
| `GET /v1/companies/{slug}` | 单个公司：它是什么、它的园区和近期新闻。 |
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
| `parties` | 涉及的公司：`name`、`role`（`developer` 开发商、`owner` 业主、`operator` 运营商、`customer` 客户、`utility` 公用事业公司、`contractor` 承包商）、`quote`、`confirmed`，以及 `ended_on` 和 `ended_reason` —— 为 null，或说明该角色何时、因何结束（以往的租户、已出售的场址）。角色已结束的客户不是该园区的买方：[Capex](capex) 和公司页面都不会给它计入任何容量。`customer` 字段保留来源的说法。 |
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

## GET /v1/projects/{id}/facts

该园区的来源所作的每一条陈述，每条一行，无论陈述的对象是什么 —— 页面上每个数字背后的主数据，包括页面没有显示的那些。参见[数值从哪里来](provenance)。

| 键 | 含义 |
| --- | --- |
| `entity`、`entity_key` | 陈述的对象：`campus` 园区；`tranche` 分期（其键）；`party` 相关方（公司的键，角色在 `field` 中）；`milestone` 里程碑（其类型）；`obstacle` 障碍（其类别）。 |
| `field`、`value`、`number`、`unit` | 字段、按原样陈述的值、可作为数字时的数值，以及单位（`MW`、`USD`、`date`……）。 |
| `tier`、`reason` | 可信程度：`confirmed`、`directory`、`derived`、`inferred`、`unconfirmed` 或 `ruled_out`，以及未确认时的原因。 |
| `held` | 是否为园区所显示的值。 |
| `as_of`、`source_url`、`source_type`、`quote`、`axes` | 何时成立、引用、原句，以及读取时的范围、界限和计量口径。 |

```json
{
  "data": [
    {
      "entity": "campus", "entity_key": "", "field": "mw_planned",
      "value": "900", "number": 900.0, "unit": "MW",
      "tier": "confirmed", "reason": null, "held": true,
      "as_of": "2026-09-01", "source_url": "https://news.example.com/north/",
      "source_type": "company_filing", "quote": "will draw 900 MW", "axes": null
    }
  ]
}
```

`entity`、`entity_key` 和 `field` 相同的多行，就是来源之间存在分歧；`held: true` 的那一行是合并时保留的值，园区页面上的[字段与证据](project-page)说明了原因。

## GET /v1/companies/{slug}

以数据形式提供的公司页面：`name`、`website`、`totals`（园区数，分别按运营商和租户身份统计，规划和运行的兆瓦数，未解决的障碍）、`states`、`phases`、它的 `projects`（每个都附有其角色、阶段、容量、障碍和 `suspected_delay`）、最近两个月报道的 `updates`，以及 `suspected_delays` —— 疑似延期园区的数量 `count`、它们合计的规划容量 `mw_planned`，以及按逾期从久到短排列的 `project_ids`。slug 与控制台中该公司页面地址里的一样：Compass Datacenters 为 `compass`，Amazon 和 AWS 都是 `amazon`。

系统关于公司本身记录的信息：`kind`（`hyperscaler`、`landlord`、`neocloud`、`ai_lab`、`utility`、`contractor`，或 null）、`parent` 和 `subsidiaries`（均为 `{name, slug}`），以及 `also_written` —— 其各园区来源使用的其他写法。母公司的数字不包括子公司的：每家公司的容量就是它自己的园区，与 [Capex](capex) 一致。

`name` 是公司唯一的名称，在每个页面上都相同；每个项目除来源原文的 `company` 和 `customer` 外，还带有 `company_name` 和 `customer_name`。`totals` 中规划和运行的兆瓦数按 [Capex](capex)（资本支出）的方式计算——疑似重复的园区只算一次，暂停和取消的园区不计入，租户只计入自己的分期——`totals.counting` 把它们拆成 `capex_row`（该公司在 `/v1/capex` 中的那一行）和 `no_named_buyer`（它建设、但没有具名买方的园区），并给出因 `duplicates`（重复）、`stopped`（暂停或取消）或计入其他买方（`elsewhere`）而被搁置的园区数量。每个项目的 `counted` 说明它贡献了什么：`status`（`counted`、`part`、`elsewhere`、`duplicate` 或 `stopped`）、`mw_planned`、`mw_built`，以及 `for`（其他买方）或 `of`（重复项所归入的园区）。

`builds` 是把该公司作为其园区建设方来计算：`campuses`、`mw_planned` 和 `mw_built` 涵盖它建设的每个园区（整体计入，不论买方是谁），不含已暂停、已取消和疑似重复的行。`totals` 则像 Capex 页面一样把每个园区计入其买方，所以建设后租出去的建设方，`totals` 小而 `builds` 大。

`new_parties` 即页面上的 *Newly mentioned suppliers*（最近被提到的新供应商）：过去 `days`（30）天内（自 `since` 起）首次被来源点名出现在这些园区之一上的公司，公司本身不计在内。`parties` 每家公司一项 —— 先列供应商（承包商和电力公司），再按日期从新到旧 —— 包含 `key`、`name`（它唯一的名称）、`roles`、`supplier`、`confirmed`（至少在一个园区上有原句支持其角色时为 true）、`latest`（它在某个园区上首次被提到的最近日期）和 `campuses`。每个园区包含 `id`、`name`、`company`（其运营方）、`place`、`state`、`role` 和 `roles`（该公司在那里的角色）、`first_cited`、`quote`、`unconfirmed`（null、`role_inferred`，或该角色为何是待确认）、`url`（文章）以及 `new_campus`（园区本身在该时间窗口内首次被报道时为 true）。角色已结束的不会列出。

## GET /v1/capex

以数据形式提供的 [Capex](capex)（资本支出）。`buyers` 中每个买方一行，包括 `(no named customer)`：

| 键 | 含义 |
| --- | --- |
| `customer`、`projects` | 买方（用其唯一的名称），以及园区数量。 |
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

`data` 包含已确认的动态，最重要的排在最前；`unconfirmed` 包含无人能引证的动态。每条动态都有 `kind`、`sign`（`good` 利好、`bad` 利空、`neutral` 中性）、`project_id`、`company`（运营商唯一的名称）、`project`、`headline`、`detail`、`happened`、`reported`、`expected`、`quote`、`source_url` 和 `publisher`。`last_crawl` 说明上次抓取文章的时间。关注列表为空时，`data` 也为空。

## GET /v1/me

密钥（`name`、`prefix`、`created_at`、`last_used_at`）及其账户（`email`、`name`）。绝不返回密钥本身。
