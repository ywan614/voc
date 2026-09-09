# Review 打标：LangExtract + Qwen

按 `docs/review_tagging_guide.md` 对评论逐观点抽取。当前没有 `example.csv`，默认使用指南所依据的 `data/sample.csv`（63 条）；可用 `--input` 指向自己的 CSV。原始数据不会被修改。

## 安装和运行

需要 Python 3.10+；本机已用 Python 3.14 创建 `.venv` 并安装依赖。新环境执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m review_tagger --dry-run
.venv/bin/python -m review_tagger --limit 3
.venv/bin/python -m review_tagger --input data/sample.csv
.venv/bin/python -m review_tagger --sample 20 --seed 20260906
.venv/bin/python -m review_tagger --last 1
.venv/bin/python -m review_tagger --last 1 --no-thinking
.venv/bin/python -m review_tagger --input data/sample.csv --max-attempts 3 --retry-no-thinking
```

其他输入：`--input data/example.csv`。自定义输出：`--output-dir data/tagging_runs/my_run`。默认创建带 UTC 时间的独立运行目录；已存在的 `reviews.jsonl` 不覆盖。`--limit` 限制调用量，`--dry-run` 不调用 API。实际运行会将评论及 few-shot 示例提交给配置的服务。

`--sample N` 不放回随机抽取 N 条，与 `--limit` 互斥。可提供 `--seed` 复现抽样；省略时自动生成随机种子并记录在运行汇总中。

## 运行日志与失败排查

日志默认开启，无需额外参数。每次运行在同一个输出目录生成：

- `run.log`：Python 标准输出、标准错误、库的 INFO 及以上日志、异常堆栈。附 UTC 时间和当前 `review_id`，终端仍同步显示。
- `events.jsonl`：结构化诊断，包括运行参数、抽样种子及选中 ID、每条开始/结束及耗时、API 请求参数、完整模型返回、API 错误、解析异常和被拒绝观点的原因。API 返回在解析之前保存，包含服务端实际返回的思考内容、`finish_reason`、usage 等字段。
- `attempts.jsonl`：逐次保存每条评论的尝试结果、次数、参数及用量，包括后续被替换的结果。
- `reviews.jsonl`：每条评论最终采用的结果，附 `attempt_count`、`selected_attempt` 和全部尝试的用量；失败记录增加 `error.message`、`error.causes` 和 `error.traceback`。异常链保存底层异常及可用的 HTTP 状态码、请求 ID、错误响应体。`rejected` 增加 schema 字段错误或原文定位信息。

例如按原方式试跑 24 条即可自动保存日志：

```bash
.venv/bin/python -m review_tagger --limit 24
```

终端第一行显示日志位置。先在 `reviews.jsonl` 查找 `status=error`、`needs_review` 或 `empty_result` 的记录，再按 `review_id` 在 `attempts.jsonl` 查看各次尝试，在 `events.jsonl` 查看 `api_error`、`api_response`、`review_failed` 或 `extraction_rejected`。`run.log` 可直接按时间阅读。

每个事件、每次尝试、每条最终结果及每行终端输出都会立即 flush；没有换行的终端片段在调用 flush 时保存。单条重试耗尽后继续处理下一条，配置或权限错误则停止整批。Ctrl+C 会记录正在处理的评论及中断堆栈，尽量导出已完成结果，退出码为 130。输入检查、模型初始化失败也记录日志。强制结束进程或断电无法补写异常或最终 CSV，但已保存的 JSONL 和开始事件可帮助定位中断位置。

日志在命令行参数解析成功后建立；`--help`、参数语法错误，以及输出目录无法创建/打开时仍只显示终端信息。`--dry-run` 也会创建日志，但不会调用 API。已有结果或日志的目录不能复用，请指定新目录。

配置中的密钥、token、密码和 Bearer 凭据会脱敏；不写认证请求头。日志包含完整评论、提示词和模型输出，按原始数据同等方式保管。旧运行未保存的错误详情无法通过新增日志恢复。

## 自动重试与合并

默认每条最多尝试 3 次，`--max-attempts N` 包含首次调用，设为 1 表示不重试。标题或正文非空但未抽取观点（`empty_result`）、校验失败（`needs_review`）、解析失败及超时、连接失败、限流、临时服务错误会重试；默认两次等待分别为 2、4 秒，增加尝试次数时等待翻倍、上限 30 秒。标题和正文均为空时标记 `empty`，不调用模型。HTTP 400、401、403、404、422 等请求、配置或权限错误停止整批，退出码为 2。

重试默认沿用首次模型和思考设置。`--retry-no-thinking` 仅在重试时关闭思考模式，`--no-thinking` 则对所有尝试关闭。空结果和校验失败的重试会附加纠错提示，但不强制生成标签。SDK 内部重试关闭，由上述逻辑统一控制次数。

首次获得非空且全部通过校验的结果后结束该条处理；重试耗尽时，优先保留首次含有效观点的部分结果，后续失败不能覆盖已有有效标签。不会把不同尝试的观点直接拼接。每条只选择一份最终结果，自动合并到 `reviews.csv`，无需另建 `retry/` 或手动合并。仍为空或失败的记录保留对应状态，汇总中明确列出；存在 `empty_result`、`needs_review` 或 `error` 时退出码为 1。

当前仍为串行、内存中处理，无自动断点续跑。重试和合并可由程序独立完成，但千万级数据仍需后续增加流式读取、持久化进度或分片处理。

## Token 用量

每次实际运行在终端显示输入、输出、总 token 数，并写入 `run_summary.json`；每条评论的 `usage` 保存小计，`api_usage` 保存服务端原始 usage（含服务商返回的缓存、推理 token 明细）。汇总每完成一条就更新，包含抽样 seed、各状态数量、已处理数量及已完成尝试次数；选中 review_id 在 `events.jsonl` 的 `run_ready` 事件中保存一次。

统计来自 Qwen API 实际返回的 `usage`，包括指南、schema、few-shot 和模型输出，非字符数估算。每条最终结果及运行汇总均包含全部尝试的用量。缓存/推理明细属于服务端报告的 token 组成，不重复加到 total_tokens。解析失败的响应也会计入。API 错误或缺失 usage 时标记 `usage_complete=false`，显示已知用量，不把未知消耗当作零；未收到 usage 的服务端消耗无法确认，因此不等同于账单审计。`--dry-run` 不请求模型，消耗为零。

## 配置

直接读取项目根目录现有的 `config.ini`，支持 `--config` 指定路径：

```ini
[openai]
openai_api_key=你的密钥
[model]
model_name=你的Qwen模型名称
model_base_url=你的OpenAI兼容API基础URL
```

基础 URL 应为服务商提供的兼容入口（通常以 `/v1` 结束），不要填完整的 `/chat/completions` 地址。代码不会替换当前配置或输出密钥，其余配置节不会被用于打标。`config.ini` 已加入 `.gitignore`。

显式构造 LangExtract 的 `OpenAILanguageModel`，避免 Qwen 名称被自动路由到其他 provider。使用 JSON 模式、温度 0；SDK 超时 90 秒，内部重试为 0，由外层 `--max-attempts` 控制。为兼容不同 Qwen 服务，不依赖服务端的严格 JSON Schema 模式；schema 注入提示词并在本地严格校验。升级 LangExtract 时需复核 `model.py` 的 SDK 超时适配（访问 1.6.0 的 `_client`）。

## 代码结构

- `review_tagger/schema.py`：独立 Pydantic schema；枚举、字段类型、行为与状态等关联约束。可用 `ReviewInsight.model_json_schema()` 导出 JSON Schema。
- `review_tagger/config.py`：读取模型名称、endpoint 和 key。
- `review_tagger/model.py`：Qwen 兼容 API 接入。
- `review_tagger/prompts.py`：加载完整指南和 schema。
- `review_tagger/examples.json`：7 组可溯源的人工示例，引用 sample.csv 原话摘录；不继承旧 tags。示例覆盖薄与缓冲、掉跟、五星负面、条件式期待、多使用者和行为状态等边界。
- `review_tagger/pipeline.py`：CSV 结构检查、抽取、schema 与原文位置校验。
- `review_tagger/retry.py`：重试判断、等待、纠错提示和最终结果选择。
- `review_tagger/__main__.py`：批量命令行入口。
- `review_tagger/run_logging.py`：终端日志、结构化诊断、异常链和凭据脱敏。
- `tests/test_tagging.py`：离线验证。

## 结果约定

每次实际运行结束时生成自动合并的 `reviews.csv`：保留输入 CSV 的全部行、原始列顺序及字段字符串（包括旧 `tags`），末尾只追加 `all_tags`。该列使用 JSON 数组保存最终选中结果中所有通过校验的完整观点（属性标签、对应原话及字符位置），不丢失标签之间的关联。空输入或重试耗尽仍无观点为 `[]`，未处理或失败为空；`needs_review` 仅包含已通过校验的观点，被拒绝的抽取及失败详情仍见 `reviews.jsonl`。抽样/限制条数时也保留全部输入行和顺序。CSV 在结束或正常 Ctrl+C 时一次性原子导出，处理过程中逐条保存 JSONL。输入已有 `all_tags` 时拒绝运行，以免覆盖原始内容。


`reviews.jsonl` 每行对应一条已处理评论，包含 `review_id`、原始字符串 `metadata`、`text`、`status`、`insights`、`rejected`、`attempt_count` 和 `selected_attempt`。`attempt_count` 计已返回结果或错误的尝试次数；中断时记录 `interrupted=true`，未完成的请求可通过开始事件定位。失败时增加 `error_type` 和脱敏后的详细 `error`。每条完成后立即写入并 flush；逐次尝试记录另存 `attempts.jsonl`。

模型仅接收 `title + "\n" + content`，不接收星级、旧 tags、theme_ids 或产品营销文案。产品名保留在 metadata，后续可据此人工确认品类。每个观点包括连续英文原话、中文属性和 `char_interval`（Python 字符索引，左闭右开，相对于完整 text）。只有与对应位置逐字匹配的结果进入 insights，无法定位或 schema 不合法的结果进入 rejected，避免混入统计。

状态：`ok` 为非空观点全部通过校验；`empty` 为空标题及正文；`empty_result` 为非空输入未抽取观点；`needs_review` 表示存在被隔离的抽取；`error` 为模型调用或解析失败。只要最终存在 `empty_result`、`needs_review` 或 `error`，进程退出码为 1；输入检查或不可重试的配置、权限错误为 2。原始 JSONL 含评论数据，应按原数据相同方式管理。

`cause` 始终表示用户归因；`unmet_need` 仅记录明确需求。整体满意度使用 `topic=整体评价` 表示，不能从星级推断。未提及字段省略，`scene` 为数组。重复原话可绑定不同属性，完全相同的观点去重。机会优先级和聚合统计不属于此次抽取输出。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m review_tagger --dry-run
```

结构与原文校验不能证明语义准确。批量用于分析前，按指南人工复核六类边界，并分别统计实际体验、预期、泛述以及不同品类。

离线测试覆盖自动重试、空结果、部分结果保留、合并后的 CSV、中断保存及用量统计；`--dry-run` 检查输入和示例，不调用 API。LangExtract 对示例中同一原话支撑两个属性的情况可能报告 fuzzy alignment 警告；这是指南允许的共享证据示例，所有示例原话另有精确子串测试，实际输出仍逐一严格校验位置。

实现依据：[LangExtract 官方仓库](https://github.com/google/langextract)、[OpenAI provider 源码](https://github.com/google/langextract/blob/main/langextract/providers/openai.py)。
