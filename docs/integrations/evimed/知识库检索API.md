# AI 检索 API V2 接口文档

> **完整调用地址：** `https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2`  
> **基础路径：** `/medicine-api/ai-api/review/api/v2`

---

## 1. 通用说明

### 1.1 请求方式

- 全部为 **POST**
- `Content-Type: application/json`
- 字符编码：**UTF-8**

### 1.2 鉴权

所有接口需在 Header 中携带 API 密钥：

```http
Authorization: Bearer {secretKey}
```

### 1.3 统一响应结构

成功：

```json
{
    "code": 200,
    "msg": "success",
    "data": {}
}
```

失败：

```json
{
    "code": 401,
    "msg": "当前api_key不存在"
}
```

| code | 含义 |
|------|------|
| 200 | 成功 |
| 400 | 参数错误 |
| 401 | 密钥不存在或未传 |
| 403 | 余额不足 |
| 429 | 请求频率过高 |
| 500 | 服务端异常 |

---

## 2. 文献 / 指南检索

### 2.1 接口信息

| 项目 | 内容 |
|------|------|
| 路径 | `POST /medicine-api/ai-api/review/api/v2/literature-guide` |
| 功能 | 按 `type` 检索文献或指南 |

### 2.2 请求参数

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | — | 检索关键词 |
| type | string | 否 | `literature` | `literature`（文献）/ `guide`（指南） |
| useLlm | boolean | 否 | `false` | `true`：模型分词；`false`：分词器分词 |
| searchBlock | boolean | 否 | `false` | 仅指南侧有效；`true` 时额外返回指南文本块 |
| count | int | 否 | `10` | 返回条数，上限 100 |
| startYear | int | 否 | — | 发表/发布年份起 |
| endYear | int | 否 | — | 发表/发布年份止 |
| articleTypes | string[] | 否 | — | 文献研究类型过滤，见下表 |
| hasPdf | boolean | 否 | — | 是否仅返回有全文的文献 |
| language | string | 否 | — | 文献语言：`zh` / `en` |
| minImpactFactor | number | 否 | — | 文献最小影响因子 |
| maxImpactFactor | number | 否 | — | 文献最大影响因子 |
| journalTiers | string[] | 否 | — | 期刊等级：`北大核心`、`科技核心`、`南大核心` 等 |
| publishers | string[] | 否 | — | 指南制定者过滤，如 `NCCN`、`CSCO` |

**articleTypes 可选值：**

`系统综述/Meta分析`、`传统综述`、`随机对照试验`、`队列研究`、`病例对照研究`、`横断面研究`、`病例系列`、`病例报告`、`专家意见和评价`、`动物实验`、`体外实验`、`指南/共识`、`经济学评价`、`其他`、`临床试验`

### 2.3 请求示例

**仅文献（默认，可不传 type）：**

```json
{
    "query": "司美格鲁肽 肥胖 2型糖尿病",
    "count": 10,
    "startYear": 2020,
    "endYear": 2025,
    "articleTypes": [
        "随机对照试验",
        "系统综述/Meta分析"
    ],
    "language": "zh"
}
```

**指南检索（含文本块）：**

```json
{
    "query": "2型糖尿病 二甲双胍",
    "type": "guide",
    "useLlm": false,
    "searchBlock": true,
    "count": 5,
    "publishers": [
        "中华医学会"
    ]
}
```

### 2.4 响应参数

#### 顶层 `data`

| 字段 | 类型 | 说明 |
|------|------|------|
| literature | object | `type=literature` 时返回 |
| guide | object | `type=guide` 时返回 |

#### `data.literature`

| 字段 | 类型 | 说明 |
|------|------|------|
| total | long | 命中总数 |
| list | array | 文献列表 |

**list 单项字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 文献 ID |
| title | string | 标题 |
| authors | string[] | 作者 |
| abstract | string | 摘要 |
| journal | string | 期刊 |
| year | string | 发表年份 |
| impactFactor | number/null | 影响因子 |
| language | string | 语言 |
| aiSummary | string | AI 摘要 |
| studyType | string[] | 研究类型 |
| coreJournals | string[] | 核心期刊标签 |
| url | object | 外链，key 为来源名（如 Pubmed、CNKI） |

#### `data.guide`

| 字段 | 类型 | 说明 |
|------|------|------|
| total | long | 命中总数 |
| list | array | 指南列表 |
| blocks | array | `searchBlock=true` 时返回文本块；否则为空数组 |

**list 单项字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 指南 ID |
| title | string | 标题 |
| year | string | 年份 |
| publisher | string | 制定者 |
| summary | string | 内容摘要 |
| publicationDate | string | 发布日期 |
| fullText | string | 全文文本（如有） |
| url | string | 详情页链接 |

**blocks 单项字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| guideId | string | 所属指南 ID |
| text | string | 文本块内容 |
| language | string | 语言 |
| title | string | 指南标题 |
| year | string | 年份 |
| publisher | string | 制定者 |

### 2.5 响应示例

**type=literature：**

```json
{
    "code": 200,
    "msg": "success",
    "data": {
        "literature": {
            "total": 128,
            "list": [
                {
                    "id": "64f1a2b3c4d5e6f7a8b9c0d1",
                    "title": "司美格鲁肽在肥胖患者中的疗效与安全性",
                    "authors": [
                        "张三",
                        "李四"
                    ],
                    "abstract": "本研究旨在评估……",
                    "journal": "中华内分泌代谢杂志",
                    "year": "2024",
                    "impactFactor": 3.52,
                    "language": "zh",
                    "aiSummary": "司美格鲁肽可显著降低体重……",
                    "studyType": [
                        "随机对照试验"
                    ],
                    "coreJournals": [
                        "北大核心"
                    ],
                    "url": {
                        "CNKI": "https://example.com/cnki/xxx",
                        "Pubmed": "https://pubmed.ncbi.nlm.nih.gov/xxx"
                    }
                }
            ]
        }
    }
}
```

**type=guide：**

```json
{
    "code": 200,
    "msg": "success",
    "data": {
        "guide": {
            "total": 12,
            "list": [
                {
                    "id": "guide_001",
                    "title": "中国2型糖尿病防治指南（2024版）",
                    "year": "2024",
                    "publisher": "中华医学会糖尿病学分会",
                    "summary": "本指南对2型糖尿病诊疗提出推荐……",
                    "publicationDate": "2024-03-01",
                    "fullText": "",
                    "url": "https://example.com/guide/guide_001"
                }
            ],
            "blocks": []
        }
    }
}
```

---

## 3. 说明书检索

### 3.1 接口信息

| 项目 | 内容 |
|------|------|
| 路径 | `POST /medicine-api/ai-api/review/api/v2/instruction` |
| 功能 | 检索药品说明书，默认范围 NMPA + FDA |

### 3.2 请求参数

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | — | 检索关键词（药品名等） |
| useLlm | boolean | 否 | `false` | `true`：模型分词；`false`：分词器分词 |
| count | int | 否 | `20` | 返回条数，上限 200 |
| source | string[] | 否 | `["nmpa","fda"]` | 数据来源：`nmpa`、`fda`、`ema`、`pmda` |

### 3.3 请求示例

```json
{
    "query": "司美格鲁肽注射液",
    "useLlm": false,
    "count": 10,
    "source": [
        "nmpa",
        "fda"
    ]
}
```

### 3.4 响应参数

`data` 按来源分组：

| 字段 | 类型 | 说明 |
|------|------|------|
| nmpa | array | 国家药监局说明书 |
| fda | array | FDA 说明书 |
| ema | array | EMA 说明书（未查询时为空数组） |
| pmda | array | PMDA 说明书（未查询时为空数组） |

**单项字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| genericNames | string | 通用名 |
| englishName | string | 英文名 |
| enterpriseName | string | 生产企业 |
| tradeNames | string | 商品名 |
| specifications | string | 规格 |
| indication | string | 适应症 |
| url | string | 详情页链接 |
| source | string | 来源：`nmpa` / `fda` / `ema` / `pmda` |

### 3.5 响应示例

```json
{
    "code": 200,
    "msg": "success",
    "data": {
        "nmpa": [
            {
                "genericNames": "司美格鲁肽注射液",
                "englishName": "Semaglutide Injection",
                "enterpriseName": "诺和诺德（中国）制药有限公司",
                "tradeNames": "诺和泰",
                "specifications": "1.34mg/ml，3ml",
                "indication": "用于2型糖尿病患者的血糖控制……",
                "url": "https://example.com/instruction?source=nmpa&name=xxx.pdf",
                "source": "nmpa"
            }
        ],
        "fda": [
            {
                "genericNames": "OZEMPIC",
                "englishName": "semaglutide",
                "enterpriseName": "Novo Nordisk",
                "tradeNames": "OZEMPIC",
                "specifications": "2 mg/1.5 mL",
                "indication": "Improve glycemic control in adults with type 2 diabetes……",
                "url": "https://example.com/instruction?source=fda&name=xxx.pdf",
                "source": "fda"
            }
        ],
        "ema": [],
        "pmda": []
    }
}
```

---

## 4. 临床试验检索

### 4.1 接口信息

| 项目 | 内容 |
|------|------|
| 路径 | `POST /medicine-api/ai-api/review/api/v2/clinical-trial` |
| 功能 | 检索临床试验数据 |

### 4.2 请求参数

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | — | 检索关键词 |
| count | int | 否 | `10` | 返回条数，上限 100 |
| registry | int | 否 | `0` | `0` ChiCTR；`1` ClinicalTrials.gov；`2` Cochrane Central |
| startYear | int | 否 | — | 注册/发表年份起 |
| endYear | int | 否 | — | 注册/发表年份止 |
| status | string[] | 否 | — | 招募状态 |
| phase | string[] | 否 | — | 试验阶段 |
| studyType | string[] | 否 | — | 研究类型（仅 `registry=0`） |
| hasArticles | int[] | 否 | — | 关联文献（仅 `registry=1`）：`0` 无 / `1` 有 |
| minSampleSize | long | 否 | — | 最小样本量 |
| maxSampleSize | long | 否 | — | 最大样本量 |
| source | string | 否 | — | Central 数据来源（`registry=2`）：PubMed、Embase、ICTRP 等 |

**phase 中文示例：** `I期临床试验`、`II期临床试验`、`III期临床试验`、`IV期临床试验`、`其他/N/A` 等  
**phase 英文示例：** `Phase 1`、`Phase 2`、`Phase 3`、`Phase 4`、`Not Applicable` 等

### 4.3 请求示例

**中文临床试验（ChiCTR）：**

```json
{
    "query": "司美格鲁肽 肥胖",
    "registry": 0,
    "count": 10,
    "startYear": 2020,
    "endYear": 2025,
    "phase": [
        "III期临床试验"
    ],
    "studyType": [
        "干预性研究"
    ]
}
```

**英文临床试验（ClinicalTrials.gov）：**

```json
{
    "query": "semaglutide obesity",
    "registry": 1,
    "count": 10,
    "status": [
        "Recruiting/招募中"
    ],
    "phase": [
        "Phase 3/3期临床试验"
    ],
    "hasArticles": [
        1
    ]
}
```

**Cochrane Central：**

```json
{
    "query": "GLP-1 receptor agonist weight loss",
    "registry": 2,
    "count": 10,
    "startYear": 2018,
    "endYear": 2025,
    "source": "PubMed"
}
```

### 4.4 响应参数

#### 通用

| 字段 | 类型 | 说明 |
|------|------|------|
| total | long | 命中总数 |
| list | array | 结果列表 |

#### registry=0 / 1（ChiCTR / ClinicalTrials.gov）list 单项

| 字段 | 类型 | 说明 |
|------|------|------|
| title | string | 试验标题 |
| registrationNo | string | 注册号 |
| status | string | 招募状态 |
| registrationDate | string | 注册日期 |
| phase | string | 试验阶段 |
| studyType | string | 研究类型 |
| sampleSize | string | 样本量 |
| conditions | array | 适应症/疾病 |
| primarySponsor | string | 主要申办方（中文） |
| interventions | array | 干预措施（英文） |
| url | string | 详情链接 |

#### registry=2（Central）list 单项

| 字段 | 类型 | 说明 |
|------|------|------|
| title | string | 标题 |
| year | string | 年份 |
| journal | string | 期刊 |
| url | string | 链接 |
| publicationType | string | 发表类型 |
| cochraneId | string | Cochrane ID |
| source | string[] | 数据来源 |
| keywords | string[] | 关键词 |

### 4.5 响应示例

**registry=0（中文）：**

```json
{
    "code": 200,
    "msg": "success",
    "data": {
        "total": 23,
        "list": [
            {
                "title": "司美格鲁肽治疗肥胖的随机对照试验",
                "registrationNo": "ChiCTR22000xxxxx",
                "status": "招募中",
                "registrationDate": "2023-06-15",
                "phase": "III期临床试验",
                "studyType": "干预性研究",
                "sampleSize": "300",
                "conditions": [
                    "肥胖",
                    "2型糖尿病"
                ],
                "primarySponsor": "某某医院",
                "url": "https://www.chictr.org.cn/showproj.html?proj=xxxxx"
            }
        ]
    }
}
```

**registry=1（英文）：**

```json
{
    "code": 200,
    "msg": "success",
    "data": {
        "total": 45,
        "list": [
            {
                "title": "A Study of Semaglutide in Adults With Obesity",
                "registrationNo": "NCT04184622",
                "status": "completed",
                "registrationDate": "2019-12-01",
                "phase": "Phase 3",
                "studyType": "Interventional",
                "sampleSize": "1961",
                "conditions": [
                    "Obesity",
                    "Overweight"
                ],
                "interventions": [
                    "Drug: Semaglutide"
                ],
                "url": "https://www.clinicaltrials.gov/ct2/show/NCT04184622"
            }
        ]
    }
}
```

---

## 5. 错误响应示例

**密钥未传 / 无效：**

```json
{
    "code": 401,
    "msg": "当前api_key不存在"
}
```

**余额不足：**

```json
{
    "code": 403,
    "msg": "余额不足"
}
```

**参数错误：**

```json
{
    "code": 400,
    "msg": "检索关键词不能为空"
}
```

```json
{
    "code": 400,
    "msg": "type 必须为 literature 或 guide"
}
```

---

## 6. 调用示例

```bash
curl -X POST 'https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2/literature-guide' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-secret-key' \
  -d '{
    "query": "司美格鲁肽 肥胖",
    "type": "literature",
    "useLlm": false,
    "count": 5
  }'
```

```bash
curl -X POST 'https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2/instruction' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-secret-key' \
  -d '{
    "query": "司美格鲁肽",
    "source": ["nmpa", "fda"]
  }'
```

```bash
curl -X POST 'https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2/clinical-trial' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-your-secret-key' \
  -d '{
    "query": "semaglutide obesity",
    "registry": 1,
    "count": 10
  }'
```
