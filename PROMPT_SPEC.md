# LLM Prompt 规格

默认模型为本地 Ollama `gemma4:latest`。三个阶段都要求 JSON Schema 输出，并且结果仍由 Pydantic 校验。

## LLM1：用户需求解析

输入：`product`、`extra_conditions`、`budget`。

输出：`UserRequirement`，包含商品关键词、一条完整搜索语句、价格范围、自然语言条件和原始输入。

规则：商品名、预算和原始输入属于确定性数据。后端会解析 `budget` 并覆盖模型的 `price_range`，同时原样恢复 `original_input`，避免模型遗漏导致后续筛选失效。模型负责将补充条件（包括“不要维修机”等否定要求）转换为 `conditions` 并构造一条完整搜索语句。

Gemma 4 实测：对 `16寸 32G+512G` 能正确输出“屏幕尺寸16寸”“内存至少32GB”“存储至少512GB”；曾遗漏 `6000-8000` 预算，因此已由后端确定性兜底。

## LLM1：商品信息提取

输入：`xianyu_item_id`、`title`、`raw_data.card_text`。

输出：`ProductConditions`，提取商品所有可确认信息；`conditions` 为独立自然语言事实，保留风险词，不补全未出现的规格。

Gemma 4 实测：能从 MacBook 卡片提取 M1、13 寸、16GB、256GB、银色、企业管理机、全原无修、外观成色一般和地点。输出缓存由标题与卡片文本哈希控制，文本未变时不会重复调用模型。

## LLM2a：单商品关注资格判断

输入：用户 `conditions` 与一件已经过预算、已推送状态筛选的商品；商品包含 ID、价格、标题、卡片原文、地点和已提取的 `conditions`。

输出：`ItemAssessment`，包含原样 ID、`worthwhile`、判断理由、风险和不确定性标记。

规则：出现明确规格冲突、用户明确排除内容或高风险时，`worthwhile=false` 并保留原始证据；信息不足但无明确冲突时，`worthwhile=true` 且 `uncertain=true`，避免搜索卡片信息不全造成漏报。价格已经由后端确定性过滤，不能作为该阶段排除依据。后端校验返回 ID 必须等于当前商品。

## LLM2b：候选池排序

输入：已通过 LLM2a 的候选池。候选池默认容纳 20 件商品（`config.yaml` 的 `analysis.candidate_pool_size` 可调整）；满池立即排序，任务完成时不足该容量的尾池也会排序。

输出：最多五个应通知的商品，含 `xianyu_item_id`、分数、排序、推荐理由、风险和不确定性标记。

规则：后端会丢弃任何不属于输入候选池的 ID。排序综合匹配程度、明确风险、信息完整度和价格；不能仅根据卖家所在地决定排序。信息不足但无明确冲突的商品可以推荐，但必须标记 `uncertain=true` 并在 `risks` 说明待确认项。

采集与判断通过受限队列并行：浏览器仍使用同一会话顺序、低频翻页；每页采集结果立即进入确定性过滤。商品信息提取和 LLM2a 默认最多 2 路并发（`analysis.initial_assessment_concurrency` 可调整），采集页缓冲容量与最大翻页数分别由 `collector.page_queue_size`、`collector.max_pages` 控制，同时不对同一浏览器会话发起并行翻页。
