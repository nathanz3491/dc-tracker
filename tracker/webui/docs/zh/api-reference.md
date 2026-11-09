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
| `GET /v1/conditions` | 适用于某个地方的规则（例如某个州暂停审批、某个镇的禁令），可筛选和分页。 |
| `GET /v1/conditions/{id}` | 单条规则：报道它的文章，以及它波及的每个园区。 |

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

`fields` 读取自 `/v1/projects/{id}/facts` 返回的同一批陈述。当 `quote_is_exact` 为 `true` 时，`quote` 就是该 `source_url` 对该字段所作园区陈述的 `quote`，因此两个端点不会互相矛盾。

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

## GET /v1/conditions

适用于某个地方、而不是某一个园区的规则 —— 某个州暂停发放新许可、某个镇禁止大型数据中心、某个电网运营商针对大负荷用户的规定、一项关税 —— 按最新报道日期从新到旧排列。只有当文章原句写明了规则本身、适用的地方和目前的进展时，这条规则才会被收录；它波及哪些园区，则由系统根据每个园区的所在地、阶段和规模计算得出，从不交给模型判断。

| 参数 | 含义 |
| --- | --- |
| `state` | 两个字母的州代码，例如 `TX`。 |
| `status` | 一个状态，或用逗号分隔的多个状态：`proposed`（已提出）、`pledged`（官员或候选人已承诺）、`passed`（已通过、尚未生效）、`in_force`（已生效）、`blocked`（被法院叫停）、`lifted`（已解除）、`rejected`（被否决或驳回）。默认返回所有状态，这样自己保存副本的程序也能得知某条规则已经结束。 |
| `scope` | 一个范围，或用逗号分隔的多个范围：`state`（州）、`county`（县）、`place`（市、镇、村或乡）、`grid_region`（电网区域）、`national`（全国）、`input`（影响所有园区的某种投入品，例如大型变压器）。 |
| `limit` | 每页行数，1–200。默认 30。 |
| `offset` | 跳过的行数。默认 0。 |

分页方式与 `/v1/projects` 相同。未知的参数或取值会返回 `400`。

| 键 | 含义 |
| --- | --- |
| `id`、`title`、`summary` | 规则的编号、带地名的简短名称，以及一句平实的说明。 |
| `kind` | `moratorium`（暂停令）、`ban`（禁令）、`permit_pause`（暂停审批）、`zoning`（分区规定）、`law`（法律）、`executive_order`（行政命令）、`ballot_measure`（投票提案）、`tax`（税收）、`tariff`（关税）、`grid_rule`（电网规定）、`pledge`（承诺）或 `other`（其他）。 |
| `need` | 它涉及的是哪方面，用障碍类别表示，例如 `permitting`（审批）或 `grid_capacity`（电网容量）。 |
| `effect` | `restricts` 表示它让建设更难（禁令、暂停、新增成本），`eases` 表示它让建设更容易（税收减免、无需审批即可建设的分区规定、允许大负荷用户自行发电的法律）。让建设更容易的规则永远不算作障碍。 |
| `scope`、`where` | 适用范围，以及用文字写出的地名：`Texas`、`Piqua, OH`、`ERCOT`。`state`、`county`、`place`、`region` 和 `input` 分别存放各个组成部分，该范围用不到的为 null。 |
| `applies_to` | `new`（已宣布或正在审批、且没有获批许可记录的园区）、`unbuilt`（尚未投运的园区）或 `all`（全部）。 |
| `min_mw` | 规模门槛（兆瓦）；规则没有门槛时为 null。 |
| `status` | 规则目前的进展，以最新一篇文章的说法为准。 |
| `effective_on`、`ends_on` | 文章写明的生效日期和终止日期。 |
| `first_reported`、`last_reported` | 最早和最新一篇相关文章的日期。 |
| `projects` | 它目前波及的园区数量。已解除或被否决的规则不波及任何园区。 |
| `market_context` | 全国性规则，或针对所有园区都要用到的某种投入品的规则，此项为 `true`。这类规则按设计不标记任何园区 —— 把每一行都标上等于什么也没说 —— 所以 `projects` 为 0。 |

```json
{
  "total": 14,
  "offset": 0,
  "limit": 30,
  "next_offset": null,
  "data": [
    {
      "id": 12,
      "title": "Texas pause on new data center permits",
      "kind": "pledge",
      "need": "permitting",
      "effect": "restricts",
      "scope": "state",
      "state": "TX",
      "county": null,
      "place": null,
      "region": null,
      "input": null,
      "applies_to": "new",
      "min_mw": null,
      "status": "pledged",
      "summary": "The governor-elect wants new data center permits paused until water and power rules are written.",
      "effective_on": null,
      "ends_on": null,
      "first_reported": "2026-11-05",
      "last_reported": "2026-11-08",
      "projects": 31,
      "where": "Texas",
      "market_context": false,
      "links": {"self": "/v1/conditions/12"}
    }
  ]
}
```

以上数值均为虚构，仅作示例。

规则的波及范围与园区自身的障碍分开存放：它不计入 `blocker`、`obstacles` 或 `confidence`，因此园区上的每个数值仍然可以追溯到一篇关于该园区本身的文章。

## GET /v1/conditions/{id}

单条规则：包含摘要中的全部内容，另外还有：

| 键 | 含义 |
| --- | --- |
| `articles` | 报道过这条规则的每篇文章，从新到旧：`url`、`title`、`published_at`、`status`（该文章所说的进展）、`summary`，以及 `quotes` —— 文章的原句，按引用用途分别列出：`rule`（规则本身）、`place`（适用地方）、`status`（进展），有门槛时还有 `threshold`（门槛）。 |
| `affected_projects` | 它波及的每个园区：`project_id`、`name`、`company`、`city`、`county`、`state`、`phase`、`fit`、`why`、`first_seen`（该园区最早被纳入的时间）和 `links.self`。 |

园区的所在地和阶段都有记录时，`fit` 为 `exact`；匹配中有一部分是推断出来的，则为 `likely`：镇一级的规则匹配的是登记在该镇名下的园区 —— 由于没有任何园区有实测的场址坐标，无法核实它是否真的在镇界之内；电网区域的规则按州推断，前提是该州大部分位于这个运营商的区域内；规模门槛遇到来源没有写明规模的园区时，同样只能算作可能。`why` 用文字说明属于哪种情况，例如 `TX · announced · no permit on record`。

```json
{
  "data": {
    "id": 12,
    "title": "Texas pause on new data center permits",
    "status": "pledged",
    "articles": [
      {
        "url": "https://news.example.com/texas-pause",
        "title": "Governor-elect calls for data center permit pause",
        "published_at": "2026-11-08T00:00:00",
        "status": "pledged",
        "summary": "The governor-elect wants new data center permits paused.",
        "quotes": {
          "rule": "The governor-elect wants a statewide pause on new data center permits until the legislature writes water and power rules.",
          "status": "She campaigned on freezing new data center construction across Texas."
        }
      }
    ],
    "affected_projects": [
      {
        "project_id": 42, "name": "North Campus", "company": "Acme Data Centers",
        "city": "Abilene", "county": "Taylor County", "state": "TX", "phase": "announced",
        "fit": "exact", "why": "TX · announced · no permit on record",
        "first_seen": "2026-11-08T18:40:12", "links": {"self": "/v1/projects/42"}
      }
    ]
  }
}
```

以上数值均为虚构，仅作示例。如果发现某条规则读错了，它会被删除，之后它的 id 会返回 `404`。

## GET /v1/me

密钥（`name`、`prefix`、`created_at`、`last_used_at`）及其账户（`email`、`name`）。绝不返回密钥本身。
