# 问卷系统 Schema 定义

- 文档版本：v1.0.0
- 更新日期：2026-11-03

本文档供问卷生成方使用，定义如何生成问卷 JSON，以及问卷提交后产生的结果 JSON。

## 1. 协议范围

| 项目 | 当前规则 |
| --- | --- |
| 文本题 | `type` 为 `"text"` |
| 单选题 | `type` 为 `"radiogroup"` |
| 多选题 | `type` 为 `"checkbox"` |
| 多页结构 | 使用 `pages` 数组声明页面及内容顺序 |
| 必填标记 | 使用 `isRequired` 声明题目是否必须提交答案 |
| “以上均无” | 多选结果使用固定答案值 `"none"` |

## 2. 生产与交付流程

1. 按照本文档生成问卷 JSON。
2. 检查必填字段、字段类型、名称唯一性和选项定义。
3. 确认题目 `name` 与选项 `value` 符合提交结果的数据契约。
4. 交付问卷 JSON，并使用第 6 节的结果 JSON 进行验收。

## 3. 问卷结构

问卷由根对象、页面对象、题目对象和选项对象组成：

```json
{
  "title": "问卷标题",
  "description": "问卷说明",
  "pages": [
    {
      "name": "page_name",
      "elements": [
        {
          "type": "radiogroup",
          "name": "question_name",
          "title": "题目标题",
          "choices": [
            {
              "value": "option_value",
              "text": "选项文本"
            }
          ]
        }
      ]
    }
  ]
}
```

### 3.1 根对象

| 字段 | 必填 | JSON 类型 | 规则 |
| --- | --- | --- | --- |
| `title` | 是 | 字符串 | 去除首尾空白后不能为空 |
| `description` | 否 | 字符串 | 问卷说明 |
| `pages` | 是 | 页面对象数组 | 至少包含一个页面 |

### 3.2 页面对象

| 字段 | 必填 | JSON 类型 | 规则 |
| --- | --- | --- | --- |
| `name` | 是 | 字符串 | 非空，所有页面中唯一 |
| `elements` | 是 | 题目对象数组 | 至少包含一个题目 |

生产建议：页面 `name` 使用稳定的 `snake_case` 标识，不要在发布后随意修改。

### 3.3 题目公共字段

| 字段 | 必填 | JSON 类型 | 默认值 | 规则 |
| --- | --- | --- | --- | --- |
| `type` | 是 | 字符串 | 无 | 仅支持 `"text"`、`"radiogroup"`、`"checkbox"` |
| `name` | 是 | 字符串 | 无 | 非空，整份问卷的所有题目中唯一 |
| `title` | 是 | 字符串 | 无 | 非空，题目标题 |
| `description` | 否 | 字符串 | 无 | 题目补充说明 |
| `isRequired` | 否 | 布尔值 | `false` | 是否必答 |

题目 `name` 是最终结果中的字段名，发布后应按稳定的数据契约管理。生产环境建议使用 `snake_case`，不要包含空格。

### 3.4 选项对象

单选题和多选题必须包含非空 `choices` 数组，选项使用对象形式。

| 字段 | 必填 | JSON 类型 | 规则 |
| --- | --- | --- | --- |
| `value` | 是 | 字符串 | 非空，同一题内唯一，作为实际答案值 |
| `text` | 否 | 字符串 | 展示文本，缺省时使用 `value` |

示例：

```json
{
  "value": "male",
  "text": "男"
}
```

`value` 是数据契约，`text` 是展示内容。修改展示文案不应改变答案值。协议允许使用中文 `value`，跨团队协作时建议使用稳定英文标识。

## 4. 三种题型

### 4.1 文本题

```json
{
  "type": "text",
  "name": "name",
  "title": "姓名",
  "description": "请填写真实姓名",
  "isRequired": true
}
```

最终答案为 JSON 字符串：

```json
{
  "name": "张三"
}
```

规则：

- 必填文本去除首尾空白后不能为空。
- 最终结果中的文本会去除首尾空白。
- 非必填空白文本不会进入最终结果。

### 4.2 单选题

```json
{
  "type": "radiogroup",
  "name": "gender",
  "title": "性别",
  "isRequired": true,
  "choices": [
    { "value": "male", "text": "男" },
    { "value": "female", "text": "女" },
    { "value": "undisclosed", "text": "不便透露" }
  ]
}
```

最终答案必须使用某个已声明的 `value`：

```json
{
  "gender": "male"
}
```

规则：

- 答案使用字符串形式的选项 `value`。
- 未作答时，最终结果不包含该题目字段。

### 4.3 多选题

```json
{
  "type": "checkbox",
  "name": "diet_behaviors",
  "title": "是否有以下饮食行为",
  "isRequired": true,
  "showNoneItem": true,
  "noneText": "以上均无",
  "choices": [
    { "value": "irregular_meals", "text": "三餐不规律" },
    { "value": "snacks", "text": "喜加餐零食" },
    { "value": "late_night_meals", "text": "夜宵频繁" }
  ]
}
```

多选专属字段：

| 字段 | 必填 | JSON 类型 | 默认值 | 规则 |
| --- | --- | --- | --- | --- |
| `showNoneItem` | 否 | 布尔值 | `false` | 是否启用“以上均无”互斥项 |
| `noneText` | 否 | 字符串 | 无 | 无项展示文案 |
| `noneValue` | 否 | 字符串 | `"none"` | 固定为 `"none"`，通常省略 |

普通多选结果为字符串数组：

```json
{
  "diet_behaviors": ["irregular_meals", "snacks"]
}
```

选择“以上均无”的结果为：

```json
{
  "diet_behaviors": ["none"]
}
```

结果规则：

- 普通答案值来自 `choices` 中的 `value`，无项答案值固定为 `"none"`。
- 未作答或空数组 `[]` 都视为未作答，非必答多选不会进入最终结果。
- `["none"]` 视为已作答，可以满足 `isRequired` 的提交要求。
- 重复值会被去重。
- 普通选项按照 `choices` 中的声明顺序返回。
- 无项结果固定为 `["none"]`。

生产建议：即使 `showNoneItem` 为 `false`，也不要把 `"none"` 用作普通选项值，以免后续启用无项时产生冲突。

## 5. 完整问卷示例

以下示例覆盖三种题型、必填与选填、多页及无项，可直接作为生产模板：

```json
{
  "title": "健康生活方式问卷",
  "description": "健康生活方式问卷生产示例",
  "pages": [
    {
      "name": "basic",
      "elements": [
        {
          "type": "text",
          "name": "name",
          "title": "姓名",
          "isRequired": true
        },
        {
          "type": "text",
          "name": "nickname",
          "title": "昵称",
          "description": "选填"
        },
        {
          "type": "radiogroup",
          "name": "gender",
          "title": "性别",
          "isRequired": true,
          "choices": [
            { "value": "male", "text": "男" },
            { "value": "female", "text": "女" },
            { "value": "undisclosed", "text": "不便透露" }
          ]
        }
      ]
    },
    {
      "name": "lifestyle",
      "elements": [
        {
          "type": "radiogroup",
          "name": "exercise_frequency",
          "title": "平均每周运动次数",
          "isRequired": true,
          "choices": [
            { "value": "never", "text": "几乎不运动" },
            { "value": "1_2", "text": "1~2 次" },
            { "value": "3_5", "text": "3~5 次" },
            { "value": "daily", "text": "几乎天天运动" }
          ]
        },
        {
          "type": "checkbox",
          "name": "diet_behaviors",
          "title": "是否有以下饮食行为",
          "isRequired": true,
          "showNoneItem": true,
          "noneText": "以上均无",
          "choices": [
            { "value": "irregular_meals", "text": "三餐不规律" },
            { "value": "snacks", "text": "喜加餐零食" },
            { "value": "late_night_meals", "text": "夜宵频繁" }
          ]
        },
        {
          "type": "checkbox",
          "name": "symptoms",
          "title": "近一周是否有以下症状",
          "showNoneItem": true,
          "noneText": "以上均无",
          "choices": [
            { "value": "abdominal_pain", "text": "腹痛" },
            { "value": "bloating", "text": "腹胀" },
            { "value": "diarrhea", "text": "腹泻" }
          ]
        },
        {
          "type": "text",
          "name": "note",
          "title": "补充说明"
        }
      ]
    }
  ]
}
```

## 6. 提交结果 JSON

提交结果根对象只包含 `answers`：

```json
{
  "answers": {
    "name": "张三",
    "gender": "male",
    "exercise_frequency": "1_2",
    "diet_behaviors": ["none"],
    "symptoms": ["abdominal_pain", "diarrhea"],
    "note": "无其他补充"
  }
}
```

结果规则：

- `answers` 的 key 是题目 `name`。
- 文本和单选答案为 JSON 字符串。
- 多选答案为 JSON 字符串数组。
- 只包含已作答题目，未作答的选填题字段直接省略。
- 文本结果会去除首尾空白。
- 单选及多选普通答案使用选项 `value`，不是展示文本 `text`。
- `["none"]` 表示明确选择“以上均无”，属于已作答。
- 多选普通选项的顺序与问卷中 `choices` 的声明顺序一致。

上述结果中，`nickname` 未作答，因此字段不出现。`name` 的原始输入即使包含首尾空白，结果中也会被去除。

## 7. 生产验收清单

### 问卷 JSON

- [ ] 根对象包含非空 `title` 和至少一个页面。
- [ ] 页面 `name` 非空且不重复，每页至少一个题目。
- [ ] 题目 `name` 非空且整卷唯一，标题非空。
- [ ] 只使用 `"text"`、`"radiogroup"`、`"checkbox"` 三种题型。
- [ ] 单选和多选至少有一个对象形式的选项。
- [ ] 选项 `value` 非空且题内唯一，`text` 类型正确。
- [ ] `isRequired`、`showNoneItem` 使用 JSON 布尔值。
- [ ] `noneValue` 省略或固定为 `"none"`。
- [ ] 启用无项时，普通选项没有使用 `"none"`。
- [ ] 问卷符合标准 JSON 语法。

### 提交结果数据契约

- [ ] 结果字段名与题目 `name` 一致。
- [ ] 文本和单选答案为 JSON 字符串。
- [ ] 多选答案为 JSON 字符串数组。
- [ ] 单选及多选普通答案使用选项 `value`，不是展示文本 `text`。
- [ ] 选填空白文本和空多选的字段可以省略。
- [ ] 多选普通选项按照 `choices` 的声明顺序返回。
- [ ] 无项结果使用 `["none"]`。
