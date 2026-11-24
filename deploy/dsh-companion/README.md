# QQ、个人微信和官方 DeepSeek Harness

普通消息先进入 AstrBot 的回复流程。群聊参与插件选中一轮后，大肥鱼和记忆插件照常修改请求。提供商通过私有 Unix 套接字调用官方 DSH 的 ACP profile。私聊和群各自绑定 DSH session，AstrBot 开新对话时创建新绑定。判断、评估和总结走独立的临时会话。

## 运行组件

- 使用 AstrBot 4.29.4、Node.js 24 和已登录的官方 DSH 0.2.1-rc.2。复制 `plugin/astrbot_plugin_dsh_companion` 到 AstrBot 的 `data/plugins/`。提供商类型为 `dsh_companion_acp`。普通聊天实例使用 `dsh_chat`，辅助实例 `dsh_judgment` 配置 `auxiliary: true`。把本地 Agent 的首选模型设为前者，群聊判断、共情评估和记忆总结选择后者。
- 桥服务用登录 DSH 的账号启动 `node server.mjs`。设置四个绝对路径环境变量 `DAFEIYU_DSH_STATE`、`DAFEIYU_DSH_SOCKET`、`DAFEIYU_DSH_BIN`、`DAFEIYU_DSH_PATCH`。最后一个指向本目录的 `cordis.patch.yml`。补丁中的 provider/model 要和自己的 DSH 账号模型标识一致。保留状态目录和 DSH 的 session 存储，备份时将二者视为一组。
- 将套接字所在目录只读挂到 AstrBot 的 `/run/dafeiyu-dsh`，套接字名为 `chat.sock`。两个进程需要能以同一 UID 访问此套接字。DSH 账号登录资料保留在宿主机。模型选择的工具由 AstrBot 用原消息身份执行。DSH ACP profile 关闭宿主机文件、Shell、工作流及子代理工具。
- 框架补丁 `deploy/patches/respond_stage.py` 的原版用于 4.29.0。部署 4.29.4 时，在补丁暂存目录对副本应用 `respond-4.29.4.patch`，以保留新版回复流水线的逻辑。其余三个大肥鱼补丁沿用原件。
- 在桥插件的 `owner_private_scopes` 填写实际主人私聊的完整 UMO。QQ 和微信分别绑定。私有阅读进度和工作笔记只对已绑定的私聊开放。把 UTF-8 文本、Markdown 或 PDF 放入 `data/plugin_data/astrbot_plugin_dsh_companion/books/` 可供主人调用书架和分段阅读工具。
- 对 AstrBot 4.29.4 的 `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py` 副本运行 `patch -p1 < qq-private-scope-4.29.4.patch`，将副本只读挂回同一位置。QQ 群临时私聊从收消息时就有独立历史，只有 OneBot 标记为好友的普通私信可以使用主人权限。
- 群聊可配 Group Chat Plus 的 `enable_group_wait_window` 和 `group_wait_window_timeout_ms`。微信使用 WeChat Native 的 `group_reply_mode=probability`、`unique_session=false`，同时关掉 AstrBot 内置概率抢答。LivingMemory 需单独选择 embedding 提供商才能启动自动记忆。DSH 绑定和书架进度可以独立使用。

## 本地中文记忆

4 核 CPU 主机可用 Hugging Face Text Embeddings Inference 的 CPU 镜像运行 `BAAI/bge-small-zh-v1.5`。模型输出 512 维，单段最多 512 个 token，适合给聊天片段和摘要做检索。AstrBot 使用 host 网络时，将向量服务只发布到宿主机回环地址。以下配置已在 Linux x86_64 的隔离环境验证。[模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5/tree/7999e1d3359715c523056ef9478215996d62a620)

| 设置 | 值 |
|---|---|
| TEI 镜像 | `ghcr.io/huggingface/text-embeddings-inference@sha256:8419f533857b503ebf6ec292a95d4f1cf9c0464ac8b8abeef39518cf110e5726` |
| 模型目录 | 只读挂载完整模型目录到 `/model`，使用 `--model-id /model --pooling cls --auto-truncate` |
| 访问和日志 | 发布 `127.0.0.1:6275:80`，用私有环境文件传入 `API_KEY`。该版本会在默认启动日志中打印参数，设置 `LOG_LEVEL=warn,text_embeddings_router::http::server=info` |
| AstrBot 嵌入提供商 | OpenAI Embedding，ID `bge_local_zh`，API Base `http://127.0.0.1:6275/v1`，模型 `BAAI/bge-small-zh-v1.5`，维度 `512`，维度参数发送模式 `auto`。使用上述内部服务的密钥 |
| LivingMemory | 嵌入选 `bge_local_zh`，总结选 `dsh_judgment`，作用域选 `session`，开启人格隔离。每 10 轮总结，一次最多召回 5 条 |

首次接入或更换模型时，同时核对实际向量维度和索引状态。启用插件后若嵌入提供商仍未就绪，回复前后可能各等待约 30 秒。

## 离线核验

运行 `node --test tests/dsh-companion.test.mjs` 和 `python -m unittest discover -s tests -p test_dsh_companion.py`。在 4.29.4 镜像内，使用空数据目录、`--network none` 和 `PYTHONPATH=仓库根目录` 运行 `python tests/dsh_provider_fat.py` 可核对真实 AstrBot SDK 的消息钩子、工具和重复消息处理。这些检查使用模拟的 DSH 回复。
