# LLM Prompt 规格

默认模型为本地 Ollama `gemma4:latest`。三个阶段都要求 JSON Schema 输出，并且结果仍由 Pydantic 校验。

## LLM1：用户需求解析

输入：`product`、`extra_conditions`、`budget`、`exclude_keywords`。

输出：`UserRequirement`，包含商品关键词、三条搜索语句、价格范围、自然语言条件、排除词和原始输入。

规则：商品名、预算、排除词和原始输入属于确定性数据。后端会解析 `budget` 并覆盖模型的 `price_range`，同时原样恢复 `exclude_keywords` 与 `original_input`，避免模型遗漏导致后续筛选失效。模型负责将自由文本条件转换为 `conditions` 和构造三条搜索语句。

Gemma 4 实测：对 `16寸 32G+512G` 能正确输出“屏幕尺寸16寸”“内存至少32GB”“存储至少512GB”；曾遗漏 `6000-8000` 预算，因此已由后端确定性兜底。

## LLM1：商品信息提取

输入：`xianyu_item_id`、`title`、`raw_data.card_text`。

输出：`ProductConditions`，提取商品所有可确认信息；`conditions` 为独立自然语言事实，保留风险词，不补全未出现的规格。

Gemma 4 实测：能从 MacBook 卡片提取 M1、13 寸、16GB、256GB、银色、企业管理机、全原无修、外观成色一般和地点。输出缓存由标题与卡片文本哈希控制，文本未变时不会重复调用模型。

## LLM2：候选比较和排序

输入：用户 `conditions` 与已经过预算、排除词、已推送状态筛选的商品列表；每件商品有 ID、价格、标题、卡片原文、地点和商品 `conditions`。

输出：最多五个推荐商品，含 `xianyu_item_id`、分数、排序、推荐理由、风险和不确定性标记。

规则：后端会丢弃任何不属于输入候选列表的 ID。信息不足但无明确冲突时允许推荐，并标记 `uncertain=true`；明确风险或规格冲突必须写入 `risks`。

Gemma 4 实测：完整匹配的 16 寸、32GB、512GB MacBook 被排在第一位；企业管理机、13 寸、16GB、256GB 的候选未被推荐。
