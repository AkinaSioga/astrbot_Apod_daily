# apod_daily

AstrBot 的 NASA APOD（Astronomy Picture of the Day，每日天文图）插件。支持手动查看、向多个 QQ 群测试推送，以及按服务器本地时间每日自动推送。图片、标题、日期和说明会通过 AstrBot 的 HTML 渲染能力合成一张大卡片发送。

插件使用 NASA 官方 APOD API，不解析 APOD HTML 页面。图片会缓存到本地，同一天多群推送只请求和翻译一次。

## 安装

将整个 `apod_daily` 文件夹放入 AstrBot 的插件目录，然后在 AstrBot WebUI 中重载或启用插件。依赖为 `httpx`；如果 AstrBot 没有自动安装依赖，请在对应 Python 环境中安装 `requirements.txt`。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | `DEMO_KEY` | NASA API Key |
| `target_group_id` | 空 | 目标 QQ 群号；多个群可用英文/中文逗号、分号、空格或换行分隔 |
| `push_time` | `14:00` | 每日推送时间，24 小时制，按 AstrBot 服务器本地时间执行 |
| `send_explanation` | `true` | 是否发送 APOD 说明 |
| `translate_enabled` | `true` | 是否使用 AstrBot 已配置的 LLM 翻译 |
| `translator_provider_id` | 空 | 翻译用 Provider，可在 WebUI 下拉选择；留空使用会话或全局默认 Provider |
| `explanation_word_limit` | `250` | 英文说明超过该单词数后不翻译全文 |

`push_time` 支持 `9:00`、`09:00`、`14:30`、`23:59`。建议设置为 `14:00` 或更晚，避免 NASA 当天内容尚未更新。

NASA `DEMO_KEY` 有较严格的请求限额。可前往 <https://api.nasa.gov/> 免费申请个人 API Key。

## 指令

- `/apod`：在当前聊天查看今日 APOD，可重复执行。
- `/apod_test`：向配置的全部目标群发送今日 APOD，用于测试主动推送。

图片类型只发送一张完整卡片，不再分别发送文字和 APOD 图片，也不会显示图片地址。卡片中包含图片、标题、日期和说明。标题和说明优先使用 LLM 中文翻译；翻译失败时回退到英文。说明超过限制时不会把全文交给 LLM，只翻译标题，并在卡片中显示“太长而截断”。

APOD 有时是视频。遇到非图片内容时，插件会发送一张提示卡片，不发送视频链接，也不会翻译说明。

卡片渲染依赖 AstrBot 自带的 `html_render()` 能力以及 WebUI 中配置的文转图服务。若日志出现“APOD 卡片渲染失败”，请先检查 AstrBot 的文转图服务是否可用。

## 缓存与防重复

插件在 `data/apod_cache.json` 保存 APOD 内容、翻译和各群推送记录，在 `data/images/` 保存当天图片。定时任务对同一个 APOD 日期只向同一个群成功推送一次；手动 `/apod` 和测试 `/apod_test` 不受此限制。

插件会在执行 `/apod` 或 `/apod_test` 时通过 `event.get_platform_id()` 自动记录当前机器人平台实例，并用它为目标群构造 AstrBot UMO。建议安装或更换 NapCat 机器人账号后先执行一次 `/apod_test`，确认主动推送正常；平台 ID 会保存在 `data/apod_cache.json` 中供定时推送复用。
"# astrbot_Apod_daily" 
