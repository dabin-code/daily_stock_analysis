# 板块查询接口文档（Boards API）

- **Base URL**：`/api/v1/boards`
- **认证**：当启用管理鉴权（`is_auth_enabled()` 为真）时，需携带登录会话 Cookie（`session`），否则返回 `401`；未启用鉴权时可匿名访问。
- **数据前提**：板块与成分关系依赖 `board_master` / `instrument_board_membership` 表，需先通过 `scripts/backfill_instrument_boards.py` 填充；股票名称来自 `instrument_master` 表，未填充时 `name` 返回 `null`。

---

## 1. 查询板块列表

`GET /api/v1/boards`

列出指定市场下的活跃板块及其成员股票数量，结果按成员数降序排列。

### 查询参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `market` | string | 否 | `cn` | 市场：`cn`/`hk`/`us` |
| `board_type` | string | 否 | 无 | 板块类型过滤，如 `industry`、`concept` |
| `min_member_count` | int | 否 | `0` | 最小成员股票数量（`>=0`） |

### 响应 `200`

```json
{
  "market": "cn",
  "board_type": "industry",
  "total": 2,
  "items": [
    { "board_id": 1, "board_name": "白酒", "board_type": "industry", "member_count": 45 },
    { "board_id": 2, "board_name": "锂电池", "board_type": "industry", "member_count": 60 }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `market` | string | 市场 |
| `board_type` | string \| null | 过滤条件回显 |
| `total` | int | 板块总数 |
| `items[].board_id` | int | 板块 ID |
| `items[].board_name` | string | 板块名称 |
| `items[].board_type` | string \| null | 板块类型 |
| `items[].member_count` | int | 成员股票数量 |

### 示例

```bash
curl "http://localhost:8000/api/v1/boards?market=cn&board_type=industry&min_member_count=10"
```

---

## 2. 查询板块成分股（单板块）

`GET /api/v1/boards/{board_name}/constituents`

根据板块名称查询该板块下的成分股代码与名称。

### 路径参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `board_name` | string | 是 | 板块名称（如 `白酒`，含中文需 URL 编码） |

### 查询参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `market` | string | 否 | `cn` | 市场：`cn`/`hk`/`us` |

### 响应 `200`

```json
{
  "market": "cn",
  "board_name": "白酒",
  "total": 2,
  "codes": ["600519", "000858"],
  "items": [
    { "code": "600519", "name": "贵州茅台" },
    { "code": "000858", "name": "五粮液" }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `market` | string | 市场 |
| `board_name` | string | 板块名称 |
| `total` | int | 成分股数量 |
| `codes` | string[] | 成分股代码列表（向后兼容） |
| `items[].code` | string | 股票代码 |
| `items[].name` | string \| null | 股票名称；股票池无记录时为 `null` |

### 错误响应

| 状态码 | 场景 | 示例 |
|--------|------|------|
| `404` | 板块不存在或无成分股 | `{"error":"not_found","message":"未找到板块 白酒 的成分股"}` |
| `500` | 服务器错误 | `{"error":"internal_error","message":"查询板块成分股失败: ..."}` |

### 示例

```bash
curl "http://localhost:8000/api/v1/boards/%E7%99%BD%E9%85%92/constituents?market=cn"
```

---

## 3. 批量查询板块成分股

`POST /api/v1/boards/constituents:batch`

一次性查询多个板块的成分股代码与名称。

### 请求体（`application/json`）

```json
{
  "board_names": ["白酒", "锂电池"],
  "market": "cn"
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `board_names` | string[] | 是 | — | 板块名称列表（至少 1 个） |
| `market` | string | 否 | `cn` | 市场：`cn`/`hk`/`us` |

### 响应 `200`

```json
{
  "market": "cn",
  "boards": [
    {
      "board_name": "白酒",
      "total": 1,
      "codes": ["600519"],
      "items": [{ "code": "600519", "name": "贵州茅台" }]
    },
    {
      "board_name": "锂电池",
      "total": 0,
      "codes": [],
      "items": []
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `market` | string | 市场 |
| `boards[].board_name` | string | 板块名称（按请求顺序返回） |
| `boards[].total` | int | 该板块成分股数量；无成分股时为 `0`（不报 404） |
| `boards[].codes` | string[] | 成分股代码列表 |
| `boards[].items` | object[] | 成分股明细（`code` + `name`） |

> 与单板块接口不同：批量接口对查不到成分股的板块返回 `total: 0`，而非 `404`。

### 示例

```bash
curl -X POST "http://localhost:8000/api/v1/boards/constituents:batch" \
  -H "Content-Type: application/json" \
  -d '{"board_names":["白酒","锂电池"],"market":"cn"}'
```

---

## 通用错误格式

```json
{ "error": "错误类型", "message": "错误详情", "detail": null }
```

| 状态码 | 含义 |
|--------|------|
| `401` | 未登录（启用鉴权时） |
| `404` | 资源不存在（仅单板块成分股接口） |
| `422` | 请求参数校验失败（如批量接口 `board_names` 为空） |
| `500` | 服务器内部错误 |

---

## 在线文档

启动服务后可访问自动生成的交互式文档，本批接口归在 **Boards** 分组下：

- Swagger UI：`/docs`
- ReDoc：`/redoc`
- OpenAPI Schema：`/openapi.json`
