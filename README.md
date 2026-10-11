# AstrBot VTube Studio Live2D 控制插件

当前版本：`1.4.0`

让 AstrBot 的 LLM 能够实时控制 VTube Studio 中的 Live2D 模型，包括触发动作热键、切换表情、注入参数等。

---

## 功能特性

| LLM 工具函数 | 说明 |
|---|---|
| `vts_get_hotkeys` | 获取当前模型所有热键列表 |
| `vts_trigger_hotkey` | 触发指定热键（播放动作、切换表情等） |
| `vts_get_expressions` | 获取所有表情及当前激活状态 |
| `vts_set_expression` | 激活/停用指定表情 |
| `vts_move_model` | 移动/旋转/缩放模型 |
| `vts_inject_parameter` | 直接注入 Live2D 参数值（精细控制） |
| `vts_get_parameters` | 获取所有可用 Live2D 参数 |
| `vts_model_info` | 获取当前模型基本信息 |

此外，插件支持“自主 Live2D 标签”机制：每次 Bot 回复前会把可用表情按键说明注入给 LLM，LLM 可在回复末尾输出 `<l2d:标签>`，插件会截获标签、从最终消息中移除，并触发对应 VTS 热键。

> 只有在插件确认 VTube Studio / Live2D 已连接可用时，才会向 LLM 注入这些标签说明；未连接时不会影响正常对话。

插件也支持读取 B站直播弹幕：连接直播间后会缓存最近弹幕/礼物/醒目留言等事件，并在 Bot 回复前把最近直播间上下文注入给 LLM，让 Bot 能根据弹幕内容自然回应。

---

## 安装

1. 将本目录放入 AstrBot 的 `data/plugins/` 目录下
2. 在 AstrBot WebUI 的插件管理页面中启用插件，依赖会自动安装

---

## 使用前提

1. **启动 VTube Studio**（Steam 版或独立版均可）
2. 在 VTube Studio 中开启 API：
   - 进入 **设置 → 常规设置 → 插件 API**
   - 将 "启动 API（WebSocket）" 开关打开
   - 默认端口为 **8001**，如有修改请在插件配置中同步

---

## 首次使用：认证

插件启动后，在聊天中发送：

```
/vts_auth
```

VTube Studio 会弹出授权窗口，点击 **允许**，认证成功后 Token 会自动保存，之后重启无需重新认证。

---

## 常用命令

```
/vts_auth     认证 VTube Studio（首次使用）
/vts_status   查看连接状态和当前模型
/vts_discover  重新扫描并自动发现 VTS 地址
/vts_list     列出所有热键和表情（方便 LLM 选择）
/vts_l2d_list  查看当前启用的自主 L2D 标签
/bili_live_start <房间号>  启动 B站直播弹幕监听
/bili_live_stop           停止 B站直播弹幕监听
/bili_live_status         查看弹幕监听状态
/bili_live_recent [数量]  查看最近缓存的直播事件
/bili_live_bind_here      将当前聊天绑定为直播弹幕自动回应输出会话
/subtitle_status          查看字幕 overlay 状态
/subtitle_test [文本]     测试打字机字幕
/subtitle_clear           清空字幕 overlay
```

---

## 自主 Live2D 标签

在插件配置中开启 `autonomous_l2d_enabled`，并在 `l2d_hotkeys` 里添加表情按键条目：

| 字段 | 说明 |
|---|---|
| `name` | 表情名称，用于在配置列表和命令中识别，例如 `开心`、`害羞` |
| `tag` | 给 LLM 使用的标签名，例如 `happy`，模型会输出 `<l2d:happy>` |
| `hotkey_id` | VTube Studio 当前模型里的热键 ID，可用 `/vts_list` 查看 |
| `description` | 表情说明，LLM 会根据这里的语气和场景描述自主选择 |
| `duration` | 持续时间，设为 `0` 表示只触发一次 |
| `release_after_duration` | 持续时间结束后是否再次触发同一个热键，适合开关型表情 |

LLM 输出示例：

```text
好呀，我已经记下来了。
<l2d:happy>
```

用户最终只会看到：

```text
好呀，我已经记下来了。
```

插件会在后台触发 `happy` 对应的 VTS 热键。如果本次不适合表情，LLM 会被提示输出 `<l2d:none>`，这个标签同样会被移除且不会触发热键。

---

## B站直播弹幕读取

参考 Super Agent Party 的直播能力，插件内置了 B站直播弹幕监听。Web 接入默认使用 [Raven95676/astrbot_plugin_bilibili_live](https://github.com/Raven95676/astrbot_plugin_bilibili_live) 中二开过的 `blivedm` 后端来解包和标准化直播事件，再交给本插件的缓存、日志、字幕和 AstrBot 原生自动回复链路处理。

### 启动方式

临时启动：

```text
/bili_live_start 123456
```

直播功能由 `bilibili_enabled` 作为总开关控制。关闭时不会自动启动、不会手动启动监听、不会向 LLM 注入弹幕上下文，LLM 工具也不可用。

或在插件配置中填写 `bilibili_room_id`，并开启 `bilibili_enabled`，插件启动时会自动监听该房间。

配置命名参考 Super Agent Party 的直播机器人：

| 字段 | 说明 |
|---|---|
| `bilibili_enabled` | 直播功能总开关，关闭后所有直播监听和注入能力都不生效 |
| `bilibili_type` | 监听类型，参考 `web` / `open_live`；两种模式都已接入 |
| `bilibili_room_id` | B站直播房间号 |
| `bilibili_web_backend` | Web 弹幕后端，默认 `blivedm`；可改为 `builtin` 使用早期内置实现排查兼容性 |
| `bilibili_sessdata` | 可选登录态 Cookie，可直接粘贴 bilibot 的完整 Cookie，也可只填 `SESSDATA` |
| `bilibili_ACCESS_KEY_ID` | `open_live` 模式必填 |
| `bilibili_ACCESS_KEY_SECRET` | `open_live` 模式必填 |
| `bilibili_APP_ID` | `open_live` 模式必填 |
| `bilibili_ROOM_OWNER_AUTH_CODE` | `open_live` 模式必填 |

`web` 模式适合公开直播间，配置房间号即可。默认的 `blivedm` 后端可识别普通弹幕、礼物、醒目留言、点赞、进场和上舰等事件；`open_live` 模式会调用 B站直播开放平台启动场次、连接返回的弹幕服务器、维持项目心跳，并在停止时关闭场次。

### LLM 可读取内容

默认会注入最近的 `danmaku` 弹幕。你也可以在 `bili_live_inject_event_types` 中加入：

| 事件类型 | 说明 |
|---|---|
| `danmaku` | 普通弹幕 |
| `gift` | 礼物 |
| `super_chat` | 醒目留言 |
| `buy_guard` | 大航海 |
| `enter_room` | 进入直播间 |
| `follow` | 关注直播间 |
| `like` | 点赞 |
| `live_start` | 开始直播 |
| `live_end` | 结束直播 |

LLM 也可以通过工具 `bili_live_recent_danmaku` 主动读取最近直播事件，适合用户问“弹幕在说什么”“回应一下观众”这类场景。

公开直播间一般不需要 Cookie；如果遇到 `code=-352`、昵称被隐藏或需要登录态，可以在配置项 `bilibili_sessdata` 中直接粘贴 bilibot 使用的完整 Cookie，或只填写浏览器 Cookie 里的 `SESSDATA`。

### 自动回应弹幕

默认情况下，插件只会捕获并缓存弹幕，不会自动让 LLM 回复。要让 Bot 主动回应直播间评论：

1. 在希望 Bot 输出回复的聊天里发送：

```text
/bili_live_bind_here
```

2. 在插件配置里开启：

```text
bili_live_auto_reply_enabled = true
```

3. 可按需调整：

| 配置项 | 说明 |
|---|---|
| `bili_live_auto_reply_mode` | 默认 `native`，把弹幕摘要投递回 AstrBot 原生事件队列 |
| `bili_live_auto_reply_cooldown_seconds` | 自动回应冷却时间，避免刷屏 |
| `bili_live_auto_reply_min_events` | 积累多少条弹幕后回应 |
| `bili_live_auto_reply_max_events` | 每次参考最近多少条弹幕 |
| `bili_live_auto_reply_system_prompt` | 控制回应语气和角色 |

`native` 模式会尽量走 AstrBot 原生回复路径，因此能吃到当前人格、世界书、记忆、TTS、分段等事件链路。重启插件后需要重新发送一次 `/bili_live_bind_here`，因为原生事件模板只保存在当前进程内。

---

## 打字机字幕 Overlay

开启 `subtitle_enabled` 后，插件会启动一个透明网页字幕层。Bot 每次回复后，插件会等待最终消息链生成完成，再把最终可见文本推送到该网页，并以打字机效果逐字显示。

默认地址：

```text
http://127.0.0.1:18081/
```

在 OBS 中添加“浏览器源”，URL 填上面的地址，背景会保持透明。你可以用命令测试：

```text
/subtitle_test 这是一条打字机字幕测试。
```

常用字幕配置：

| 配置项 | 说明 |
|---|---|
| `subtitle_enabled` | 字幕总开关 |
| `subtitle_port` | 本地字幕网页端口 |
| `subtitle_typing_speed_ms` | 每个字符出现的间隔 |
| `subtitle_hold_seconds` | 打完字后的停留时间 |
| `subtitle_max_length` | 字幕最大长度，避免遮挡 |
| `subtitle_font_size` | 字号 |
| `subtitle_text_color` | 字幕颜色 |
| `subtitle_stroke_color` | 描边颜色 |
| `subtitle_position` | `bottom` / `center` / `top` |

字幕会默认清理 `<l2d:...>`、CQ 码和常见尖括号控制标签，避免控制指令出现在画面上。如果回复里包含语音组件，默认只显示语音后面的普通文本，适合“日语语音 + 中文字幕”的输出方式。

---

## TTS 语音嘴型联动

开启 `mouth_sync_enabled` 后，插件会在最终消息链中等待 TTS 语音组件生成完成，读取本地 `wav` 音频，按音量包络驱动 VTube Studio 嘴部参数。这样语音、字幕和嘴型会以“语音文件已生成”为同步起点一起开始。

常用配置：

| 配置项 | 说明 |
|---|---|
| `mouth_sync_enabled` | 嘴型联动总开关，默认关闭 |
| `mouth_sync_open_parameter` | 嘴部开闭参数，默认 `ParamMouthOpenY` |
| `mouth_sync_form_parameter` | 嘴型变形参数，可填 `ParamMouthForm`，留空则不驱动 |
| `mouth_sync_fps` | 每秒推送参数次数，建议 `20~30` |
| `mouth_sync_gain` | 音量增益，越大嘴张得越明显 |
| `mouth_sync_smoothing` | 平滑程度，越大越柔和但响应越慢 |
| `mouth_sync_noise_gate` | 静音阈值，减少底噪导致的微张嘴 |
| `mouth_sync_form_strength` | 嘴型变形强度 |

测试命令：

```text
/mouth_sync_test 2
```

注意：当前插件内嘴型联动优先读取本地 `wav` 文件。如果 TTS 插件把语音注册成远程 URL，或生成的是无法被 Python 标准库直接读取的格式，嘴型联动会跳过；这种场景仍可使用 VTube Studio 自带麦克风/虚拟声卡方案。

---

## LLM 工具调用示例

配置好 LLM 后，直接对话即可控制 Live2D：

> 「让模型开心地挥手」→ LLM 自动调用 `vts_trigger_hotkey` 触发对应热键  
> 「切换成害羞表情」→ LLM 调用 `vts_set_expression` 激活对应表情文件  
> 「让模型向左移动」→ LLM 调用 `vts_move_model` 控制位置  
> 「让模型微笑」→ LLM 调用 `vts_inject_parameter` 设置 `MouthSmile` 参数  

---

## 插件配置

在 AstrBot 插件配置中可设置：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `vts_host` | `localhost` | VTube Studio 所在主机地址 |
| `vts_port` | `8001` | VTube Studio API 端口 |
| `auto_discover` | `true` | 自动发现 VTS 地址（推荐开启） |
| `auto_connect` | `true` | 插件启动时自动认证 |
| `autonomous_l2d_enabled` | `true` | 启用自主 Live2D 标签机制 |
| `l2d_max_tags_per_reply` | `1` | 每次回复最多触发的 L2D 标签数量 |
| `l2d_hotkeys` | `[]` | Live2D 表情按键条目列表 |
| `bilibili_enabled` | `false` | B站直播功能总开关 |
| `bilibili_type` | `web` | B站直播监听类型，支持 `web` / `open_live` |
| `bilibili_room_id` | `0` | B站直播房间号 |
| `bilibili_web_backend` | `blivedm` | Web 弹幕后端，默认使用参考插件的二开 `blivedm` |
| `bilibili_sessdata` | `""` | 可选的 B站完整 Cookie 或 SESSDATA |
| `bilibili_ACCESS_KEY_ID` | `""` | `open_live` 模式必填 |
| `bilibili_ACCESS_KEY_SECRET` | `""` | `open_live` 模式必填 |
| `bilibili_APP_ID` | `""` | `open_live` 模式必填 |
| `bilibili_ROOM_OWNER_AUTH_CODE` | `""` | `open_live` 模式必填 |
| `bili_live_inject_enabled` | `true` | 向 LLM 注入最近直播弹幕 |
| `bili_live_inject_max_events` | `8` | 每次注入的最大直播事件数 |
| `bili_live_inject_event_types` | `["danmaku"]` | 注入给 LLM 的事件类型 |
| `bili_live_cache_size` | `80` | 内存中缓存的直播事件数量 |
| `bili_live_log_events` | `true` | 将捕获到的直播事件写入 AstrBot 日志 |
| `bili_live_auto_reply_enabled` | `false` | 启用直播弹幕自动回应 |
| `bili_live_auto_reply_mode` | `native` | 自动回应模式，默认走 AstrBot 原生事件队列 |
| `mouth_sync_enabled` | `false` | 启用 TTS 语音嘴型联动 |
| `mouth_sync_open_parameter` | `ParamMouthOpenY` | 嘴部开闭参数 |
| `mouth_sync_form_parameter` | `""` | 可选嘴型变形参数 |
| `mouth_sync_fps` | `30` | 嘴型参数更新帧率 |
| `mouth_sync_gain` | `1.6` | 音量到开嘴幅度的增益 |
| `mouth_sync_smoothing` | `0.45` | 嘴型平滑程度 |
| `subtitle_enabled` | `false` | 启用打字机字幕 overlay |
| `subtitle_host` | `127.0.0.1` | 字幕服务监听地址 |
| `subtitle_port` | `18081` | 字幕服务端口 |
| `subtitle_typing_speed_ms` | `45` | 打字速度 |
| `subtitle_hold_seconds` | `4` | 字幕停留时间 |
| `subtitle_max_length` | `120` | 字幕最大长度 |
| `subtitle_strip_tts_blocks` | `true` | 字幕移除整段 TTS 语音文本 |
| `subtitle_voice_use_following_plain` | `true` | 有语音时只显示语音后的普通文本 |
| `debug_mode` | `false` | 输出详细调试日志 |

---

## 更新记录

### 1.4.2

- 新增 TTS 语音嘴型联动：等待最终语音组件生成后，读取本地 wav 音频并驱动 VTS 嘴部开闭参数。
- 新增 `mouth_sync_*` 配置和 `/mouth_sync_test` 测试命令。
- 字幕与嘴型现在共用最终消息链阶段作为同步起点，更适合语音生成后再显示字幕和动嘴。

### 1.4.1

- Web 弹幕监听默认改用 Raven95676/astrbot_plugin_bilibili_live 的二开 `blivedm` 后端，提升弹幕、礼物、SC、点赞、进场和上舰事件解析稳定性。
- 新增 `bilibili_web_backend`，可在 `blivedm` 与早期 `builtin` 后端之间切换。
- 自动回应仍保持 `native` 原生 AstrBot 事件路径，继续吃人格、世界书、TTS 和其它插件链路。

### 1.4.0

- 新增透明网页字幕 overlay，支持 OBS 浏览器源叠加到 Live2D 画面。
- 新增打字机字幕效果，Bot 回复会逐字显示并自动停留/淡出。
- 新增 `/subtitle_status`、`/subtitle_test`、`/subtitle_clear` 命令。
- 新增字幕样式配置：打字速度、停留时间、字号、颜色、描边、位置、最大长度等。

### 1.3.1

- 补完整 B站直播 `open_live` 模式：支持开放平台启动场次、WebSocket 鉴权、项目心跳、停止时关闭场次。
- 支持开放平台弹幕、礼物、SC、大航海、点赞、进入直播间、开播、下播事件。
- `bilibili_type` 现在可在 `web` 和 `open_live` 之间切换。

### 1.3.0

- 新增 B站直播弹幕读取能力，可通过 `/bili_live_start <房间号>` 启动监听。
- 新增最近直播事件缓存、`/bili_live_recent` 查看命令、`bili_live_recent_danmaku` LLM 工具。
- 支持在 LLM 回复前注入最近直播弹幕上下文，让 Bot 能读弹幕并按需回应。
- 直播配置命名参考 Super Agent Party：`bilibili_enabled`、`bilibili_type`、`bilibili_room_id`、`bilibili_sessdata`。

### 1.2.1

- 新增自主 Live2D 标签机制：回复前注入可选表情说明，回复后截获 `<l2d:标签>` 并触发 VTS 热键。
- 新增 `l2d_hotkeys` 条目式配置，可配置标签、热键 ID、说明、持续时间和是否自动结束。
- 新增 `/vts_l2d_list` 命令查看当前启用的标签条目。
- 标签会从最终回复中移除，用户不会看到控制标签。
- 只有在 Live2D/VTS 已连接可用时才注入标签提示词，未连接时不干扰普通聊天。

---

## 常见问题

**Q: 提示"未连接到 VTube Studio"**  
A: 确认 VTube Studio 已启动并开启了 WebSocket API，然后发送 `/vts_auth` 进行认证。

**Q: LLM 不知道有哪些热键/表情可用**  
A: 发送 `/vts_list` 查看列表，并将结果告知 LLM（或放入 System Prompt），LLM 就能精准调用。

**Q: 想让 LLM 持续控制面部参数（如眨眼动画）**  
A: 需要持续调用 `vts_inject_parameter`，建议在场景中使用。

---

## 目录结构

```
astrbot_plugin_vtube_studio/
├── __init__.py          # 包入口
├── main.py              # 插件主体（Star 类 + LLM 工具注册）
├── vts_client.py        # VTube Studio WebSocket 客户端封装
├── vts_discovery.py     # 跨平台自动发现模块
├── metadata.yaml         # 插件元数据
├── _conf_schema.json     # 插件配置 Schema
├── requirements.txt     # Python 依赖
└── README.md            # 说明文档
```

---

## License

MIT
