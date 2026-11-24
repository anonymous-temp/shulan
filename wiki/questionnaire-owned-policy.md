# Questionnaire 自有生成政策

Questionnaire 的整份问卷选题与呈现政策由本域 `modules/questionnaire/generation_policy.py` 拥有。原先直接使用 Record 私有题库和校验函数，Record 调整快捷问题或校验时，会在问卷任务版本未变的情况下改变问卷输入、选项和模型输出验收。本次从 `efca880` 基线原样冻结实际用到的叶子规则，移除这条运行时依赖；Record 流程、公共会话 gate 和主体协作没有改动。

## 固定内容

`questionnaire-generation-policy.v1 / questionnaire-generation-policy-v1` 保存有序生活习惯题目的 key、题干、单选/多选类型、选项及桥接文字。问卷生成器、规划模型输入和因素模型的 lifestyleKey 过滤共用本域固定内容。没有复制 Record 的阶段计数、问答生命周期、画像类型映射或整份 application 模块。

呈现校验保留严格题目 Schema、选项规范化、常规 ask_intake 题目的多选/单选要求、拒答快捷键限制、单一语义选项及答案入口/内部失败文案检查。异常仍为 ValueError 子类并保留原规则 code/hint，原有问卷流程继续跳过无效规划结果。Prompt 文件、模型调用数量、JSON 键序、题序、选项顺序和发布字段保持原有行为。

## 受理和恢复

新 `questionnaireAgentManifest` 使用 v3，并显式保存 `generationPolicySelection`；生成前和答后因素任务执行前沿既有清单绑定检查该选择。原无清单任务、完整旧 v1 和缺少政策字段的已知 v2 仅映射冻结 v1，不改原持久清单、原始答案、输入指纹、assessmentId 或恢复预算。当前 v3 缺失政策、显式 null、未知修订和 latest 别名均拒绝；旧 v2 显式带有不支持政策同样拒绝，不以历史格式掩盖未知版本。

因素任务原有持久清单与 checkpoint 继续受同一任务租约约束；先验证固定清单，再使用 checkpoint。生成不支持的版本沿既有不可重试 QUESTIONNAIRE_GENERATION_FAILED，因素评估沿 FACTOR_EVALUATION_INVALID；普通查询仍只读。将来改动本域政策必须提供新修订及明确执行路由，不能直接改写 v1 或借 Record 当前实现恢复旧任务。

## 验证

离线回归核对冻结题目、选项和校验边界；修改 Record 私有题库、桥文、题型和校验器后，新建问卷、规划/因素模型输入与已固定问卷输出不变。另验证旧 v2 无 GET 后台恢复、旧因素 checkpoint 重放、当前/旧格式的未知政策在 OCR/模型前拒绝，以及本次消费者没有 Record 私有 import。既有 Questionnaire 全域回归及完整 tests/unit 继续作为交付门禁；不代表真实模型质量或部署验收。

主 Agent 入口与恢复语义见 [Questionnaire 主 Agent](questionnaire-main-agent.md)。
