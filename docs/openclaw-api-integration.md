# OpenClaw 热点题材筛选接口文档

## 概述

DSA 提供一个给 OpenClaw 调用的专用接口，用于触发 `extreme_strength_combo` 选股任务。

- 接口路径固定为 `POST /api/v1/screening/openclaw-theme-run`
- 当前只支持 A 股市场，即 `market="cn"`
- 接口收到请求后，会固定使用 `extreme_strength_combo` 策略执行筛选
- 请求中的 `trade_date` 会按原值传入筛选任务
- 传入的 `themes` 会被解析为对应板块的成分股，并将本次选股池**锁定**为这些成分股；选股仅在传入题材对应的板块内进行，不再扫描全市场，也不再走热点板块热度计算/收窄流程
- 题材→板块解析复用内置的 `ThemeNormalizationService`（别名表 + 板块名召回）
- 若传入题材无法匹配到任何带成分股的板块，接口会返回 `422 validation_error`（不会静默回退到全市场）

## 请求定义

### Endpoint

```http
POST /api/v1/screening/openclaw-theme-run
Content-Type: application/json
```

认证方式不在该接口函数内单独声明，实际是否需要认证取决于部署侧的全局鉴权配置。

### 请求体

```json
{
  "trade_date": "2026-03-27",
  "market": "cn",
  "themes": [
    {
      "name": "AI芯片",
      "heat_score": 92,
      "confidence": 0.88,
      "catalyst_summary": "政策催化叠加产业事件，AI 芯片方向快速升温",
      "keywords": ["AI", "芯片", "算力", "先进封装"],
      "evidence": [
        {
          "title": "政策发布",
          "source": "新华社",
          "url": "https://example.com/news/1",
          "published_at": "2026-03-27T08:30:00+08:00"
        }
      ]
    }
  ],
  "options": {
    "candidate_limit": 50,
    "ai_top_k": 10,
    "force_refresh": false
  }
}
```

## 参数说明

### 顶层字段

| 字段 | 类型 | 必填 | 约束 / 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `trade_date` | `string` | 是 | 必须是 `YYYY-MM-DD` | 交易日，接口会先校验格式，再传给筛选任务 |
| `market` | `string` | 是 | 当前只能是 `"cn"` | 市场标识，Phase 1 只支持 A 股 |
| `themes` | `OpenClawTheme[]` | 是 | 不能为空数组 | 热点题材列表，至少 1 个 |
| `options` | `OpenClawScreeningOptions` | 否 | 省略时使用默认值 | 运行参数 |

### `OpenClawTheme`

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | `string` | 是 | 无额外范围限制 | 题材名称 |
| `heat_score` | `number` | 是 | `0 <= heat_score <= 100` | 题材热度分 |
| `confidence` | `number` | 是 | `0 <= confidence <= 1` | 题材置信度 |
| `catalyst_summary` | `string` | 是 | 无额外范围限制 | 题材催化摘要 |
| `keywords` | `string[]` | 是 | 可为空数组，但不建议 | 题材关键词 |
| `evidence` | `OpenClawEvidence[]` | 否 | 默认 `[]` | 新闻/公告等证据列表 |

### `OpenClawEvidence`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | `string` | 是 | 新闻标题 |
| `source` | `string` | 是 | 来源 |
| `url` | `string \| null` | 否 | 原文链接 |
| `published_at` | `string \| null` | 否 | 发布时间，建议使用 ISO 8601 |

### `OpenClawScreeningOptions`

| 字段 | 类型 | 必填 | 默认值 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `candidate_limit` | `integer` | 否 | `50` | `1 <= candidate_limit <= 200` | 候选上限 |
| `ai_top_k` | `integer` | 否 | `10` | `0 <= ai_top_k <= 50`，且不能大于 `candidate_limit` | AI 二次分析数量 |
| `force_refresh` | `boolean` | 否 | `false` | 无额外约束 | 预留字段，当前接口已接收但暂未实际生效 |

## 成功响应

### 200 OK

```json
{
  "run_id": "screening_run_cn_2026-03-27_xxxxx",
  "status": "completed",
  "strategy_names": ["extreme_strength_combo"],
  "accepted_theme_count": 1,
  "created_at": "2026-03-28T06:05:59.123456+00:00"
}
```

### 响应字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | `string` | 本次筛选任务 ID |
| `status` | `string` | 任务状态，直接透传筛选服务返回值 |
| `strategy_names` | `string[]` | 固定为 `["extreme_strength_combo"]` |
| `accepted_theme_count` | `integer` | 本次接受的热点题材数量 |
| `created_at` | `string` | 接口返回时间，ISO 8601 格式 |

## 错误响应

### 400 Bad Request

#### 不支持的市场

```json
{
  "error": "unsupported_market",
  "message": "market must be 'cn' in phase 1"
}
```

#### 题材为空

```json
{
  "error": "invalid_themes",
  "message": "themes cannot be empty"
}
```

#### 日期格式错误

```json
{
  "error": "invalid_date_format",
  "message": "trade_date must be in YYYY-MM-DD format"
}
```

### 422 Unprocessable Entity

#### 请求体字段缺失或字段范围不合法

这类错误由 FastAPI / Pydantic 模型校验返回，格式如下：

```json
{
  "error": "validation_error",
  "message": "请求参数验证失败",
  "detail": [
    {
      "loc": ["body", "trade_date"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

#### `ai_top_k > candidate_limit`

这是接口内的业务校验，返回格式如下：

```json
{
  "error": "validation_error",
  "message": "ai_top_k cannot be greater than candidate_limit"
}
```

### 500 Internal Server Error

```json
{
  "error": "internal_error",
  "message": "Internal server error"
}
```

## 调用示例

### cURL

```bash
curl -X POST http://localhost:8000/api/v1/screening/openclaw-theme-run \
  -H "Content-Type: application/json" \
  -d '{
    "trade_date": "2026-03-27",
    "market": "cn",
    "themes": [
      {
        "name": "AI芯片",
        "heat_score": 92,
        "confidence": 0.88,
        "catalyst_summary": "政策催化叠加产业事件，AI 芯片方向快速升温",
        "keywords": ["AI", "芯片", "算力", "先进封装"],
        "evidence": [
          {
            "title": "政策发布",
            "source": "新华社",
            "url": "https://example.com/news/1",
            "published_at": "2026-03-27T08:30:00+08:00"
          }
        ]
      }
    ],
    "options": {
      "candidate_limit": 50,
      "ai_top_k": 10,
      "force_refresh": false
    }
  }'
```

### Python

```python
import requests

url = "http://localhost:8000/api/v1/screening/openclaw-theme-run"

payload = {
    "trade_date": "2026-03-27",
    "market": "cn",
    "themes": [
        {
            "name": "AI芯片",
            "heat_score": 92,
            "confidence": 0.88,
            "catalyst_summary": "政策催化叠加产业事件，AI 芯片方向快速升温",
            "keywords": ["AI", "芯片", "算力", "先进封装"],
            "evidence": [],
        }
    ],
    "options": {
        "candidate_limit": 50,
        "ai_top_k": 10,
        "force_refresh": False,
    },
}

resp = requests.post(url, json=payload, timeout=30)
print(resp.status_code)
print(resp.json())
```

## 结果查询

拿到 `run_id` 后，可继续查询：

```http
GET /api/v1/screening/runs/{run_id}
GET /api/v1/screening/runs/{run_id}/candidates
GET /api/v1/screening/runs/{run_id}/candidates/{code}
```

## 当前实现备注

1. `strategy_names` 不是请求参数，接口内部固定为 `["extreme_strength_combo"]`
2. `mode` 不是请求参数，接口内部固定以 `balanced` 模式调用筛选任务
3. `stock_codes` 不是请求参数；接口内部会把传入的 `themes` 解析为板块成分股并锁定为选股池（解析不到则报错 `422`），不再跑全市场股票池
4. `force_refresh` 当前只是保留位，代码已接收但暂未驱动额外刷新逻辑
5. 返回的 `status` 不是固定写死的 `queued`，而是实际筛选服务返回值
