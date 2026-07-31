# dc-tracker 使用指南

追踪美国数据中心建设项目，每一个数字都能追到出处。

这份指南按"你想做什么"组织，不按模块。命令都可以直接复制运行。

---

## 目录

- [五分钟上手](#五分钟上手)
- [日常怎么用](#日常怎么用)
- [网页控制台](#网页控制台)
- [放到公网上](#放到公网上)
- [读懂输出：三个必须理解的概念](#读懂输出三个必须理解的概念)
- [哪些命令花钱](#哪些命令花钱)
- [导出](#导出)
- [命令速查](#命令速查)
- [出问题时](#出问题时)

---

## 五分钟上手

```bash
tracker init        # 建库或升级表结构，已有数据不会动
tracker stats       # 看看库里现在有什么
tracker serve       # 打开网页控制台
```

`tracker init` 可以随时重复运行。它按顺序执行 `migrations/` 下的 SQL，已经跑过的跳过。

现在库里的样子（写这份文档时的真实数据）：

```
124 projects, 264 citations
planned capacity: 35,156 MW across 89 project(s)
announced investment: $213.6B
```

注意最后一行下面那句提示：**这些总量只统计了有出处的数字，是下限，不是行业总和**。有 35 个项目没人报过装机容量，它们不算 0，而是不参与求和。

---

## 日常怎么用

### 一条命令跑完整个循环

```bash
tracker sync
```

它依次做四件事：

1. **discover** —— 拉取 `seed/feeds.toml` 里的新闻源，用关键词筛标题，把可能相关的文章排进队列。这一步不抓正文，不调模型，**不花钱**。
2. **extract** —— 抓取文章正文，交给模型抽取字段，通过证据校验后写库。**这一步花钱**，默认上限 25 篇。
3. **refresh** —— 重新读取超过 14 天没看过的旧引用，让已有项目的数据跟着更新，而不是只增不改。同样**花钱**，默认上限 25 篇。
4. **list** —— 打印结果。

常用参数：

```bash
tracker sync --limit 10                 # 只抓 10 篇新文章，省钱
tracker sync --dry-run                  # 预演，什么都不写、不花钱
tracker sync --skip-refresh             # 只找新项目，不回头更新旧的
tracker sync --deep                     # 顺带扫 sitemap 存档挖老项目，免费
```

### 想自己控制每一步

`sync` 只是把下面四步串起来。拆开跑的好处是可以在花钱之前先看一眼：

```bash
tracker discover                  # 只找候选，免费
tracker queue                     # 看看找到了什么标题
tracker queue --drop --url <URL>  # 明显不相关的，先删掉
tracker ingest crawl --from-queue --limit 10   # 确认没问题再抓，这步花钱
```

`tracker queue` 显示的是**标题**而不只是网址，这是故意的——光看网址判断不了一篇文章值不值一次模型调用。

筛选故意做得比较宽松：**宁可多收再人工筛，也不要把关键词规则调到把真项目也丢掉**。所以队列里出现无关文章是正常的，不是 bug。

### 死磕某一个项目

```bash
tracker enrich 93
```

`sync` 是把预算摊到整个库，`enrich` 反过来——针对一个项目，把所有能用的方法轮一遍：先推导、再查队列、重试失败的、翻存档、搜索、刷新旧引用。一轮填不出新东西就自动停。

```bash
tracker enrich 93 --dry-run     # 先看它打算做什么
tracker enrich 12 44 88         # 一次多个项目，共用一份预算
```

### 不花钱就能补的字段

```bash
tracker ingest geo
```

用美国人口普查局的对照表推导 `county`、`lat`、`lon`。不用 API key，不调模型，没有单条成本。

它拒绝猜两件事：横跨多个县的城市（休斯顿、奥斯汀各跨四个县）留空不填；每一个推导出来的坐标都在自己的出处里写明**那是城镇中心，不是项目地址**。

---

## 网页控制台

```bash
tracker serve
```

打开 <http://127.0.0.1:8765/>，六个页面：

| 页面 | 用来干什么 |
|---|---|
| **Projects** | 全部字段的表格，每个值下面有下划线标出处等级，鼠标悬停看原句 |
| **Map** | 地图。气泡面积是有出处的装机容量，虚线空心圈表示没人报过容量 |
| **Queue** | 待读的标题，以及抓不下来的站点 |
| **Coverage** | 字段覆盖率、必需项目清单、各类障碍卡住多少产能 |
| **Commands** | 命令面板，直接在网页上跑 |
| **Runs** | 跑过的命令和它们的完整输出 |

**和 `tracker export html` 的区别**：`export html` 生成一个单文件网页，可以直接发邮件，但它是导出那一刻的快照，也不能执行任何命令。控制台是个服务器，每次请求都重新读库，并且能跑命令。两个都有用。

几个细节：

- 表格里点任意一行打开抽屉，里面是这个项目的全部证据。按 <kbd>Esc</kbd> 关闭。
- 抽屉里的"五条轨道"来自后端计算，和 `tracker show <id>` 的结果**逐行一致**——网页不会自己另算一套。
- `--no-run` 启动的话，页面只能看，不能执行任何命令。
- 全部资源都在本地，不连任何 CDN，断网也能用。

---

## 放到公网上

控制台默认只绑定 `127.0.0.1`。想从外面访问：

```bash
# 先在 .env 里设密码（.env 已经在 .gitignore 里）
# TRACKER_CONSOLE_PASSWORD=你的密码

tracker serve --tunnel
```

会打印一个 `https://xxx.trycloudflare.com` 的公网地址。命令停掉，地址立刻失效。

**没设密码的话这条命令会直接拒绝执行**，不是警告。原因很直接：这个地址后面是一个能跑命令、能写数据库、能花钱的进程。

密码保护做了这些：

- 未登录时，**整个站点**只返回登录页，API 一律 401 —— 连前端 JS 都不给。
- 密码比对用 `hmac.compare_digest`，不会因为响应时间泄漏。
- 单个 IP 错 8 次锁 15 分钟；**所有 IP 加起来**错 40 次也锁 15 分钟。第二条是关键——只按 IP 限速的话，攻击者换一批地址就绕过去了。
- 会话 cookie 是 `HttpOnly` + `SameSite=Lax`，脚本读不到，别的网站也没法带着它来发请求。

> 有一点要清楚：`trycloudflare.com` 的域名是随机的，但**不是秘密**——它走过网络，Cloudflare 也知道。它是"不容易被撞见"，不是访问控制。真正挡人的是密码和上面那套限速。

不用了就 <kbd>Ctrl</kbd>+<kbd>C</kbd>，隧道跟着关。

---

## 读懂输出：三个必须理解的概念

这三个是这个工具存在的理由，不理解的话看到的数字容易误读。

### 1. 证据等级 —— 这个值凭什么

PRD 里那条规则是核心：**不能直接把 AI 的回答当作事实**。所以每个非空字段都带一个等级：

| 等级 | 含义 | 网页上的样子 |
|---|---|---|
| `reported` | 文章里有一句原话支持它，而且那句话确实出现在抓下来的正文里 | 实线下划线 |
| `derived` | 从人口普查数据推导出来的，确定性的，但不是"有人这么说过" | 灰色点线 |
| `unconfirmed` | **待确认**：模型抽出来了，但找不到可核对的原句 | 琥珀色点线 |
| `inferred` | 模型基于已有事实做的判断（`tracker infer` 的产物） | 紫色虚线 |
| `defaulted` | 没有任何人说过，字段又非空，摆在那里的是表结构的默认值 | 灰色虚线 |
| `missing` | NULL | 淡化 |

现在这个库里：934 个 `reported`、197 个 `derived`、170 个 `unconfirmed`、37 个 `defaulted`。

`defaulted` 和 `unconfirmed` 的区别不是抠字眼。`phase` 是非空字段，没人提到阶段时它会落到默认值 `announced`。把这个标成"待确认"等于说**有个来源声称过它但证明不了**——可事实上根本没人声称过。

**空值不一定是缺口**。`blocker` 大部分项目本来就没有，`customer` 自建园区本来就没有外部客户，`mw_built` 在还没开工的项目上填 NULL 才是对的。

在网页上把鼠标放到任意一个值上，会弹出支持它的那句原话。如果显示"来自来源摘要，不是这个字段自己的句子"，说明这条引用是在 0007 号迁移之前记录的——那时候逐字段的原句没有存下来，只能退回展示整段摘要，页面会明说，不会拿一整段假装是某个数字的出处。

### 2. 五条轨道 —— 项目到底走到哪一步

PRD 里最难的那个问题是**判断一个项目究竟走到了哪一步**。一个 `phase` 枚举回答不了，因为进度不是一条梯子，是五条**互不依赖**的轨道：

| 轨道 | 里程碑 |
|---|---|
| 买地 site control | announced → land_acquired |
| 审批 permits | permit_filed → permit_approved |
| 电力接入 power | interconnection_agreement → energized |
| 施工 construction | site_work → groundbreaking → equipment_install |
| 客户/资金 commercial | first_customer |

一个园区可以土地全款买下，同时在并网排队里卡了四年。用一个枚举看，两者都叫"在建"。

**"电力"这条轨道故意不做推断**。看到施工进度可以反推它一定拿到了地和许可（不然楼盖不起来），但**绝不会**反推它拿到了并网协议——先盖楼后等电是这一轮的常态，一个盖好的空壳在等变电站，恰恰是这个工具最该暴露的信号。

由此还能回答"接下来出现什么信号才算在推进"：**被卡住那条轨道上的下一个未达成里程碑**。

```bash
tracker show 40
```

```
| track                          | reached            | blocked by |
| site control (买地)            | complete           | -          |
| permits (审批)                 | complete (implied) | -          |
| power (电力接入)               | unknown            | -          |
| construction (施工)            | groundbreaking     | -          |
| customer & finance (客户/资金) | unknown            | -          |

watch for: a signed interconnection agreement, or a utility filing naming the
substation serving the site
```

`(implied)` 的意思是**推出来的，不是读到的**——推断不是引用，所以标出来。

### 3. 置信度 0–3

按来源权重、独立域名数、彼此是否一致来算，每次都重新计算，不存死。

**一个来源永远到不了 3**，不管它多权威。独立性按域名数，不按行数。低于 2 的会进 `tracker review`。

```bash
tracker review              # 列出置信度 ≤1 的项目，说明为什么低
tracker review --verify 3   # 记录"我人工核过 3 号了"
```

`--verify` 写的是 `last_verified_at`，和 `updated_at` 是两回事：后者表示"有字段变了"，前者表示"有人说它是对的"。

---

## 哪些命令花钱

只有五个命令会调用 LLM：

```
sync        enrich        infer        search        ingest crawl
```

其余全部免费。控制台里这五个带 `llm` 标记，**必须手动把命令名打一遍才能执行**——手滑点不出一次消费。

控成本的办法：

```bash
tracker sync --dry-run       # 预演，一分钱不花
tracker sync --limit 5       # 每篇文章一次调用，这就是上限
tracker discover             # 只找候选，免费
tracker queue --drop --url … # 花钱之前先把噪音删掉
tracker ingest geo           # 免费补 county / lat / lon
tracker sync --deep          # 免费翻存档
```

---

## 导出

```bash
tracker export md    > tracker.md              # Markdown 表格
tracker export csv   > tracker.csv             # 列顺序是固定契约
tracker export json  > tracker.json            # 带完整引用结构
tracker export html --out data/exports/x.html  # 单文件网页，双击就能开
```

同样的数据导出两次，结果**逐字节一致**——排序固定、JSON 键排序、换行统一为 LF。方便直接进 git 看 diff。想带时间戳就加 `--stamp`。

JSON 里每个项目都有 `basis`（字段 → 等级）和 `prov`（字段 → 等级 + 原句 + 是哪条引用），不用自己再推一遍。

---

## 命令速查

**看数据**

```bash
tracker list                          # 表格，默认按 MW 排
tracker list --state VA --phase construction
tracker list --risk transmission      # 卡在电网工程上的项目
tracker show 3                        # 单个项目全部细节和引用
tracker risks                         # 所有未解决的障碍，按类型分组
tracker exposure                      # 各类障碍后面压着多少产能
tracker stats                         # 汇总
tracker gaps                          # 字段覆盖率
tracker verify                        # 对照必需项目清单
```

**加数据**

```bash
tracker ingest manual --json seed/sample-projects.json
tracker ingest pjm --csv data/raw/pjm.csv --iso pjm
tracker ingest crawl --urls urls.txt
tracker ingest geo
```

**全局参数**

```bash
tracker --db path/to/other.db list    # 换一个库
tracker --json stats                  # 机器可读输出
tracker -v sync                       # 调试日志
```

`--json` 目前支持 `version`、`list`、`show`、`stats`、`gaps`。空库时也会输出合法 JSON，不会变成一段散文。

---

## 出问题时

**`no such table: risk`**
表结构旧了。`tracker init`，数据不会丢。

**`another tracker run is already writing to this database`**
SQLite 只允许一个写入者。等前一个跑完，或者停掉它。锁文件里写了持有者的 pid；进程已经死了的话会自动回收。

**队列一直空的**
先 `tracker discover`。如果还是空，看 `tracker queue --failed`——很多站点（比如 DataCenterDynamics）文章页在 Cloudflare 后面返回 403。它们的标题仍然有价值：告诉你**哪些项目存在**，事实可以再从运营商自己的新闻稿去拿。这个项目不会去绕过对方的访问控制。

**`ingest geo` 说文件缺失**
它会把两个人口普查文件的下载地址打出来（合计 3.8 MB，已在 .gitignore 里）。

**控制台打不开 / 一片空白**
先看终端有没有报错。`tracker serve` 启动时会检查前端文件是否齐全，缺了会直接报出文件名。

**忘了控制台密码**
改 `.env` 里的 `TRACKER_CONSOLE_PASSWORD`，重启 `tracker serve`。会话是存在内存里的，重启即全部失效。

**被登录锁定了**
等 15 分钟，或者重启 `tracker serve`——计数器也在内存里。

---

## 还没做的事

写这份文档时的已知缺口，避免你踩：

- 必需的 30 个项目清单还没填。`seed/required-projects.txt` 是空的，`tracker verify` 会告诉你缺哪些。
- `iso_maps.py` 里 ERCOT / CAISO 的列名是推测的，没拿真实导出文件验证过。
- 构建出来的 wheel 里**不包含 migrations**（`migrations/` 在 `tracker/` 包外面），所以非 editable 安装跑 `tracker init` 会失败。目前是 editable 安装，没暴露出来。
- 网页控制台在手机上能用但不好用：表格 13 列在窄屏上要横向翻很久。地图、抽屉、Runs 页面在手机上是正常的。
