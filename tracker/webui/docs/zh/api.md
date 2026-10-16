# API 入门

API 让程序读取你在控制台中看到的内容：园区、园区的各项数字及其背后的来源、公司、按买方统计的资本支出，以及你关注列表中的新闻。API 只读：程序通过它做的任何操作都不会改动数据。它以 JSON 格式返回结果。

| | |
| --- | --- |
| **地址** | `{{api}}` |
| **身份验证** | API 密钥，以 `Authorization: Bearer <key>` 的形式发送 |
| **速率限制** | 每个密钥每分钟 60 次请求 |
| **格式** | JSON，UTF-8 |

[API 参考](api-reference)列出了所有接口。

## 获取密钥

1. 登录控制台，打开[你的账户](account)（右上角你的名字）。
2. 在 **API keys**（API 密钥）下，给密钥起一个你认得出的名字，例如“分析笔记本”“报表服务器”，然后点击 **Create key**（创建密钥）。
3. **请立即复制密钥。** 密钥以 `dct_` 开头，只显示这一次：追踪系统只保存它的指纹，因此无法再次向你显示。如果丢失了，请撤销它，再新建一个。

一个账户最多可以有五个有效密钥。Account（账户）页面上的列表显示每个密钥的名称、开头几个字符、创建时间、最近使用时间，以及一个 **Revoke**（撤销）按钮。

> [!WARNING]
> 密钥以你的身份读取数据。不要把它写进你分享的代码、网页或网址中；像保管密码一样保管它。一旦泄露，请撤销它，它会立即失效。

密钥只在你的账户有效时才能使用。如果管理员停用了你的账户，账户下的所有密钥都会失效；如果账户被删除，其密钥也随之删除。

API 权限也由管理员按账户开启或关闭。未使用邀请码注册的账户默认关闭：Account（账户）页面会注明这一点，开启之前没有 **Create key**（创建密钥）按钮。如果之后被关闭，你的密钥会失效但仍然保留，重新开启后原样恢复可用。

## 第一个请求

```bash
curl -H "Authorization: Bearer dct_your_key_here" \
  "{{api}}/projects?state=TX&limit=5"
```

用 Python：

```python
import requests

API = "{{api}}"
HEADERS = {"Authorization": "Bearer dct_your_key_here"}

page = requests.get(f"{API}/projects", headers=HEADERS, params={"state": "TX", "limit": 5}).json()
for campus in page["data"]:
    print(campus["id"], campus["name"], campus["mw_planned"])
```

除了 `Authorization` 请求头，也可以使用 `X-API-Key: <key>`。放在网址里的密钥（`?key=`）一律不被接受：网址会留在日志和浏览器历史记录中。

## 读懂返回结果

- **每个数值都注明来源。** 园区详情中的每个字段都给出 `value`、`label`，以及其背后的 `quote` 和 `source_url`。这些标签与控制台一致：`quoted`、`derived`、`unconfirmed`（待确认）、`inferred`、`default`、`missing`、`not_applicable`。参见[数值从哪里来](provenance)。
- **数字是下限，日期的精确程度与其来源一致。** 数字的 `bound` 说明它是“大约”还是“至少”，`basis` 说明它是哪一种兆瓦（MW）；日期的 `date_precision` 说明来源给出的是某一天、某个月、某个季度、半年还是一年。参见[数字与日期](numbers-and-dates)。
- **为空往往是正确的。** `null` 表示没有任何来源陈述这个数值；参见[为什么字段为空](empty-fields)。
- **行可能被合并。** 被发现与另一个园区重复的园区会被合并进那个园区，它的 id 随即失效：API 会返回 404 并说明这一点。请按名称重新查找。

## 分页

`/v1/projects` 每次最多返回 200 个园区（默认 30 个）。每次返回都带有 `total`、`offset`、`limit` 和 `next_offset`；用 `offset=<next_offset>` 再次请求，直到 `next_offset` 为 `null`。

## 错误

每个错误都是 JSON，包含一个固定的错误码和一句说明：

```json
{"error": {"code": "key_invalid", "message": "that API key is not valid or was revoked"}}
```

| 状态码 | 错误码 | 含义 |
| --- | --- | --- |
| 400 | `bad_parameter`、`unknown_parameter`、`bad_id`、`bad_slug` | 请求的内容 API 无法理解。说明中会指出具体是什么。 |
| 401 | `key_required`、`key_invalid` | 没有密钥，或者密钥错误、已撤销，或所属账户已被停用。 |
| 403 | `api_access_off` | 密钥正确，但其所属账户的 API 权限已被关闭。请联系管理员。 |
| 404 | `not_found` | 没有这个接口、园区或公司。 |
| 405 | `read_only` | 使用了 GET 以外的方法。API 从不修改数据。 |
| 429 | `rate_limited` | 一分钟内超过 60 次请求。请等待 `Retry-After` 中给出的秒数。 |
| 500 | — | 我们这边出了故障。请稍后再试。 |

每次返回都带有 `X-RateLimit-Limit` 和 `X-RateLimit-Remaining`，程序可以据此在被拒绝之前放慢速度。

## 数据有多新

API 读取的数据与控制台相同，每天傍晚（北京时间）更新。请求之间不做缓存，因此每次返回反映的都是请求那一刻的数据。参见[数据如何保持更新](how-it-is-kept-current)。

## 稳定性

在 `v1` 内，字段可能会**增加**，但绝不会改名或删除。会导致程序出错的改动将以 `v2` 的形式推出，与 `v1` 并存，并在[更新说明](release-notes)中发布通知。
