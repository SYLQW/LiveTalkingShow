# LiveTalking 接口协议

当前接口由 `aiohttp.web` 提供，协议包含 HTTP JSON、multipart、WebSocket 二进制流和 WebRTC SDP offer/answer。

默认地址：

```text
LiveTalking  http://127.0.0.1:8050
测试 TTS      http://127.0.0.1:8036
```

LiveTalking JSON 响应：

```json
{"code": 0, "msg": "ok", "data": {}}
```

错误响应通常为：

```json
{"code": -1, "msg": "error message"}
```

说明：

- LiveTalking 多数业务错误仍返回 HTTP 200，需要检查 `code`。
- WebRTC offer 参数错误可能返回 HTTP 400。
- 测试 TTS 服务使用 `success`、`error`、`message` 等字段，见第 9 节。

## 1. 接口总览

### 1.1 核心运行接口

| 方法 | 路径 | 类型 | 作用 |
| --- | --- | --- | --- |
| `POST` | `/alpha/session` | JSON | 创建或复用 alpha session。 |
| `POST` | `/alpha/speak` | JSON | 输入文字，由 LiveTalking 调 TTS 后驱动数字人。 |
| `GET` | `/alpha/input/audio` | WebSocket JSON + binary | 接收外部 TTS 推来的 PCM 音频。 |
| `GET` | `/alpha/ws` | WebSocket binary | 输出 alpha 视频帧。 |
| `GET` | `/alpha/audio` | WebSocket binary | 输出 LiveTalking 侧音频 PCM16。 |
| `POST` | `/alpha/close` | JSON | 关闭默认或指定 alpha session。 |
| `POST` | `/interrupt_talk` | JSON | 打断指定 session。 |
| `POST` | `/is_speaking` | JSON | 查询指定 session 是否正在说话。 |

### 1.2 可选和调试接口

| 方法 | 路径 | 类型 | 作用 |
| --- | --- | --- | --- |
| `GET` | `/alpha/tuning` | JSON | 查询 Wav2Lip 运行时贴回区域参数。 |
| `POST` | `/alpha/tuning` | JSON | 更新 Wav2Lip 运行时贴回区域参数。 |
| `GET` | `/motion/clips` | JSON | 查询说话动作或静息动作素材。 |
| `POST` | `/motion/plan` | JSON | 根据文本生成动作编排计划。 |
| `POST` | `/motion/source/upload` | multipart | 上传动作素材源视频。 |
| `POST` | `/motion/source/probe` | JSON | 读取源视频时长、帧率和预览地址。 |
| `POST` | `/motion/source/detect` | JSON | 检测源视频或图片目录首帧人脸框和生成框。 |
| `GET` | `/motion/source/video` | file | 预览允许目录内的源视频。 |
| `POST` | `/motion/select` | JSON | 选择当前 session 使用的动作素材。 |
| `POST` | `/motion/clips/create` | JSON | 从源视频或图片目录生成动作素材。 |
| `POST` | `/motion/clips/update` | JSON | 修改动作素材元信息。 |
| `POST` | `/motion/clips/delete` | JSON | 删除动作素材。 |
| `GET` | `/api/admin/config` | JSON | 查看启动配置（返回 `vars(opt)` 全量配置字典）。 |
| `GET` | `/api/admin/sessions` | JSON | 查看活跃 session（返回 `sessionid`、`speaking`、`recording`、`model`、`avatar_id`、`REF_FILE`、`transport`、`batch_size`、`customopt` 等字段）。 |
| `POST` | `/api/avatar/task` | JSON/multipart | 创建 avatar 制作任务。 |
| `GET` | `/api/avatar/task/{task_id}` | JSON | 查询 avatar 任务。 |
| `DELETE` | `/api/avatar/task/{task_id}` | JSON | 删除 pending 状态的 avatar 任务。 |
| `GET` | `/api/avatar/tasks` | JSON | 列出 avatar 任务。 |

### 1.3 WebRTC 接口

| 方法 | 路径 | 类型 | 作用 |
| --- | --- | --- | --- |
| `POST` | `/offer` | WebRTC JSON | 普通 WebRTC 音视频输出。 |
| `POST` | `/alpha/webrtc/packed_offer` | WebRTC JSON | packed 单视频轨透明输出。 |
| `POST` | `/alpha/webrtc/offer` | WebRTC JSON | 双视频轨 color/alpha 透明输出。 |

### 1.4 兼容接口

| 方法 | 路径 | 类型 | 作用 |
| --- | --- | --- | --- |
| `POST` | `/human` | JSON | legacy 文本输入，需要已有 session。 |
| `POST` | `/humanaudio` | multipart | legacy 音频文件输入，需要已有 session。 |
| `POST` | `/set_audiotype` | JSON | 设置自定义动作状态。 |
| `POST` | `/record` | JSON | 开始或结束录制。 |
| `GET` | `/record/{sessionid}` | file | 下载录制 MP4。 |
| `POST` | `/close_session` | JSON | 关闭指定 session。 |

## 2. alpha session

```http
POST /alpha/session
Content-Type: application/json
```

请求：

```json
{
  "reuse": true,
  "sessionid": "",
  "session": {
    "avatar": "avatar3d2",
    "refaudio": "",
    "reftext": "",
    "custom_config": ""
  }
}
```

字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `reuse` | `true` | 复用默认 alpha session；`false` 会新建 session。 |
| `sessionid` | 空 | 指定 session ID；为空时自动生成或复用默认值。 |
| `session.avatar` | 启动配置里的 `AVATAR_ID` | avatar 目录名，对应 `data/avatars/<avatar>`。 |
| `session.refaudio` | 空 | 兼容旧音色字段。 |
| `session.reftext` | 空 | 兼容旧提示文本字段。 |
| `session.custom_config` | 空 | 自定义动作配置 JSON 字符串。 |

`avatar`、`refaudio`、`reftext`、`custom_config` 也可以直接放在请求顶层。

返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "sessionid": "123456"
  }
}
```

关闭：

```http
POST /alpha/close
Content-Type: application/json
```

```json
{"sessionid": "123456"}
```

`sessionid` 为空时关闭默认 alpha session。

## 3. 文本驱动

```http
POST /alpha/speak
Content-Type: application/json
```

请求：

```json
{
  "sessionid": "123456",
  "text": "你好，我是桌面数字人。",
  "type": "echo",
  "interrupt": true,
  "tts": {
    "voice_id": 0,
    "mode": "instruct2",
    "prompts": "请自然清晰地朗读。"
  }
}
```

字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `sessionid` | 默认 alpha session | 目标 session。 |
| `text` | 必填 | 要朗读的文本。 |
| `type` | `echo` | `echo` 直接朗读；`chat` 调 `llm_response` 后朗读。 |
| `interrupt` | `true` | 是否先打断当前朗读。 |
| `tts.voice_id` | `0` | TTS 音色 ID。 |
| `tts.mode` | `ROBOTTTS_MODE` | TTS 模式，例如 `instruct2`。 |
| `tts.prompts` | 空 | TTS 指令或提示词。 |
| `tts.ref_file` | 可选 | 兼容字段，会映射到音色。 |
| `tts.ref_text` | 可选 | 兼容字段，会映射到提示词。 |

返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "sessionid": "123456"
  }
}
```

链路：

```text
控制端 -> POST /alpha/speak
LiveTalking -> WS /tts/ws
TTS -> PCM16 chunks
LiveTalking -> /alpha/ws 视频
```

## 4. 外部音频流驱动

```http
GET /alpha/input/audio?sessionid=123456&interrupt=1
Upgrade: websocket
```

query：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `sessionid` | 默认 alpha session | 目标 session。 |
| `interrupt` | `1` | `1` 表示收到 start 时打断当前朗读；`0` 表示不断。 |

流程：

```text
TTS -> LiveTalking TEXT JSON start
LiveTalking -> TTS TEXT JSON started
TTS -> LiveTalking BINARY PCM chunks
TTS -> LiveTalking TEXT JSON end
```

start（服务端读取的有效字段为 `type`、`task_id`、`text`、`sample_rate`、`channels`、`sample_width`，其余字段可携带但会被忽略）：

```json
{
  "type": "start",
  "task_id": "demo-001",
  "text": "这段音频由外部 TTS 生成。",
  "sample_rate": 16000,
  "channels": 1,
  "sample_width": 2
}
```

started：

```json
{
  "type": "started",
  "task_id": "demo-001",
  "sessionid": "123456"
}
```

binary：

```text
PCM16 little-endian，推荐 16000 Hz / mono / signed 16-bit
```

end：

```json
{
  "type": "end",
  "task_id": "demo-001",
  "reason": "completed"
}
```

可能的错误消息：

```json
{"type": "error", "code": "InvalidRequest", "message": "message must be JSON"}
{"type": "error", "code": "NotFound", "message": "session not found"}
{"type": "error", "code": "InvalidState", "message": "binary audio received before start"}
```

## 5. alpha 视频和音频输出

```text
WS /alpha/ws?max_height=720&fps=25&format=jpeg&quality=80
WS /alpha/audio
```

`/alpha/ws` query：

| 参数 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_width` | `0-4096` | `0` | 当前客户端最大宽度，`0` 不限制。 |
| `max_height` | `0-4096` | `0` | 当前客户端最大高度，`0` 不限制。 |
| `fps` | `0-60` | `0` | 当前客户端帧率限制，`0` 不限制。 |
| `format` / `frame_format` | `bgra` / `bgra8` / `raw` / `rgba` / `rgba8` / `jpeg` / `jpg` / `png` / `webp` | `raw` | 视频帧编码。`rgba`、`rgba8` 等价于 `raw`；本机 overlay 推荐 `bgra`。 |
| `quality` | `1-100` | `80` | `jpeg` / `webp` 质量。 |

每条视频 binary message：

```text
24 byte little-endian header + payload
```

header：

| 字节 | 类型 | 含义 |
| --- | --- | --- |
| `0-3` | `char[4]` | `LTAF` |
| `4` | `uint8` | version，当前 `1` |
| `5` | `uint8` | `1=raw RGBA8`，`2=JPEG`，`3=PNG`，`4=WebP`，`5=raw BGRA8` |
| `6-7` | `uint16` | flags，当前 `0` |
| `8-11` | `uint32` | width |
| `12-15` | `uint32` | height |
| `16-23` | `uint64` | seq |

payload：

| format | payload |
| --- | --- |
| `raw` | `RGBA8`，每像素 `R,G,B,A` 4 字节。 |
| `bgra` / `bgra8` | `BGRA8`，每像素 `B,G,R,A` 4 字节。 |
| `jpeg` / `jpg` | JPEG 图片字节，不保留透明。 |
| `png` | PNG 图片字节，可保留透明。 |
| `webp` | WebP 图片字节，可保留透明。 |

`/alpha/audio` 输出：

```text
WebSocket binary PCM16 chunks，16000 Hz mono
```

## 6. 状态、动作和录制

打断：

```http
POST /interrupt_talk
Content-Type: application/json
```

```json
{"sessionid": "123456"}
```

查询说话状态：

```http
POST /is_speaking
Content-Type: application/json
```

```json
{"sessionid": "123456"}
```

返回：

```json
{"code": 0, "msg": "ok", "data": true}
```

设置动作状态：

```http
POST /set_audiotype
Content-Type: application/json
```

```json
{
  "sessionid": "123456",
  "audiotype": 1
}
```

录制：

```http
POST /record
Content-Type: application/json
```

```json
{"sessionid": "123456", "type": "start_record"}
```

```json
{"sessionid": "123456", "type": "end_record"}
```

下载：

```text
GET /record/{sessionid}
```

关闭指定 session：

```http
POST /close_session
Content-Type: application/json
```

```json
{"sessionid": "123456"}
```

运行时视觉调优（Wav2Lip 贴回区域）：

```http
GET /alpha/tuning?sessionid=123456
POST /alpha/tuning
Content-Type: application/json
```

GET 只查询当前配置。POST 会更新运行时 pads。

POST JSON body：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `sessionid` | 默认 alpha session | 目标 session。 |
| `pads` | 无 | 四值数组 `[top, bottom, left, right]`，如 `[0, 10, 0, 0]`。 |
| `top` / `bottom` / `left` / `right` | `0` | 四边独立值，优先级低于 `pads`。 |

POST 示例：

```json
{
  "sessionid": "123456",
  "pads": [0, 10, 0, 0]
}
```

POST 也可以使用独立字段：

```json
{
  "sessionid": "123456",
  "top": 0,
  "bottom": 10,
  "left": 0,
  "right": 0
}
```

返回示例：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "sessionid": "123456",
    "pads": [0, 10, 0, 0],
    "source_width": 405,
    "source_height": 720,
    "base_bbox": {"x1": 120, "y1": 180, "x2": 220, "y2": 280},
    "padded_bbox": {"x1": 120, "y1": 180, "x2": 220, "y2": 290}
  }
}
```

仅 Wav2Lip 模型支持运行时调优，其他模型返回 `"current avatar does not support tuning"` 错误。

## 7. 动作素材接口

动作素材接口用于把一段源视频切成可复用的说话动作或静息动作。新版 avatar-local 格式会把动作和配置都放在 `data/avatars/<avatar_id>/` 下：

avatar 支持两种格式：

| 格式 | 识别方式 | 动作素材位置 |
| --- | --- | --- |
| 基础格式 | `data/avatars/<avatar_id>/` 下没有 `motion.json` | 兼容旧外置目录 `data/speaking_actions/<avatar_id>/<action_id>`、`data/idle_actions/<avatar_id>/<action_id>`。 |
| avatar-local 动作格式 | `data/avatars/<avatar_id>/` 下存在 `motion.json` | `data/avatars/<avatar_id>/motions/speaking/<action_id>`、`data/avatars/<avatar_id>/motions/idle/<action_id>`。 |

```text
data/avatars/<avatar_id>/motion.json
data/avatars/<avatar_id>/motions/speaking/<action_id>/
data/avatars/<avatar_id>/motions/idle/<action_id>/
```

每个动作目录本身也是一套基础 avatar 片段，至少包含 `full_imgs/`、`face_imgs/`、`coords.pkl`。

安全限制：

- `source` 只能指向允许目录内的文件，默认允许 `tmp/uploaded_sources` 和 `data`。
- 可以用 `MOTION_ALLOWED_SOURCE_DIRS` 调整允许目录，多个目录使用系统路径分隔符。
- 上传文件大小默认不超过 `MOTION_UPLOAD_MAX_BYTES=536870912`。
- `ffmpeg` 和 `ffprobe` 默认从 `PATH` 查找，也可以用 `FFMPEG_PATH`、`FFPROBE_PATH` 或请求参数指定。
- `action_id`、`avatar_id` 只能是简单目录名，不能包含路径分隔符或 `..`。

查询素材：

```http
GET /motion/clips?sessionid=123456&kind=speaking&reload=1
```

query：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `sessionid` | 默认 alpha session | 目标 session。存在 session 时返回该 session 当前加载的素材。 |
| `kind` | `speaking` | `speaking` 表示说话动作，`idle` 表示静息动作。 |
| `avatar_id` | 启动配置 | 没有 session 时用于从磁盘列出指定 avatar 的素材。 |
| `reload` | `0` | `1` 表示让当前 session 重新从磁盘读取素材。 |

返回中的每个素材会包含 `action_id`、`display_name`、`kind`、`fps`、`frame_count`、`img_size`、`pads`、`tags`、`best_for`、`play_mode`、`can_reverse`、`weight`、`min_cycles`、`max_cycles`、`switch_at_boundary`、`enabled`、`current`、`selected`、`pool_selected` 等字段。

上传源视频：

```http
POST /motion/source/upload
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 说明 |
| --- | --- |
| `file` | 视频文件，支持 `.mp4`、`.mov`、`.webm`、`.mkv`、`.avi`。 |

返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "source": "/abs/path/to/tmp/uploaded_sources/source.mp4",
    "filename": "source.mp4",
    "size": 123456
  }
}
```

读取源视频信息：

```http
POST /motion/source/probe
Content-Type: application/json
```

```json
{
  "source": "/abs/path/to/tmp/uploaded_sources/source.mp4",
  "ffmpeg_path": "ffmpeg"
}
```

返回会包含 `duration`、`fps`、`width`、`height`、`frame_count`、`video_url`。`video_url` 可用于浏览器预览。

预览源视频：

```http
GET /motion/source/video?source=/abs/path/to/tmp/uploaded_sources/source.mp4
```

该接口只返回允许目录内的视频文件，不能用来读取服务器上的任意本地文件。

检测首帧人脸框：

```http
POST /motion/source/detect
Content-Type: application/json
```

```json
{
  "source": "/abs/path/to/tmp/uploaded_sources/source.mp4",
  "time": 0,
  "pads": [0, 10, 0, 0],
  "chroma_key": true
}
```

`source` 可以是允许目录内的视频文件或图片目录。返回会包含 `base_box`、`padded_box`、`width`、`height`、`image`。`image` 是预览图的 data URL，前端用它画“检测框”和“生成框”。

生成动作素材：

```http
POST /motion/clips/create
Content-Type: application/json
```

```json
{
  "sessionid": "123456",
  "avatar_id": "teacher_avatar",
  "kind": "speaking",
  "source": "/abs/path/to/tmp/uploaded_sources/source.mp4",
  "action_id": "lecture_explain",
  "display_name": "普通讲解",
  "start": 0,
  "end": 4,
  "fps": 15,
  "img_size": 256,
  "pads": [0, 10, 0, 0],
  "face_det_batch_size": 8,
  "max_frames": 0,
  "tags": "speaking,teaching",
  "best_for": "讲解知识点",
  "play_mode": "forward",
  "can_reverse": false,
  "weight": 3,
  "min_cycles": 1,
  "max_cycles": 1,
  "switch_at_boundary": true,
  "enabled": true,
  "chroma_key": true,
  "use_ffmpeg_cut": true,
  "ffmpeg_path": "ffmpeg"
}
```

说明：

- 默认不传 `out_root` 时，会写入新版 avatar-local 目录 `data/avatars/<avatar_id>/motions/<kind>/<action_id>`，并自动创建或更新 `data/avatars/<avatar_id>/motion.json`。
- 如果显式传入 `out_root=data/speaking_actions` 或 `out_root=data/idle_actions`，会写入旧版外置目录，用于兼容旧素材管理方式。
- `source` 可以是允许目录内的视频文件，也可以是允许目录内的图片目录。图片目录中的帧必须同尺寸、同通道；`/motion/source/probe` 和 `/motion/source/video` 只支持视频文件。
- `max_frames=0` 表示不限制帧数。交互测试建议使用短素材，避免启动时一次性加载太多帧。
- `play_mode` 支持 `forward`、`pingpong`、`reverse`、`random_direction`。`forward` 只正放；`pingpong` 正放后倒放；`reverse` 只倒放；`random_direction` 每次选中时随机正放或倒放。`reverse` 和 `random_direction` 需要配合 `can_reverse=true` 才会倒放；`pingpong` 本身会倒放，不受 `can_reverse` 限制。
- `strategy` 只决定自动素材池如何选择下一段素材，不控制播放方向。`weighted_no_repeat` 配合 `play_mode=pingpong` 时仍会正放再倒放；要禁用倒放，设置该素材 `play_mode=forward`。
- `weight` 是自动素材池权重，范围 `0-1000`。`min_cycles` 和 `max_cycles` 范围 `1-100`。
- `switch_at_boundary=true` 表示音频目标状态变化时，动作素材等当前原子动作播放到边界再切换；嘴型推理仍会随音频立即生效。`enabled=false` 表示该素材不加入 `auto` 自动素材池。
- 如果请求里带了有效 `sessionid`，生成后会让该 session 重新加载对应素材。
- 同名素材默认不覆盖，除非传入 `overwrite=true`。

异步生成动作素材：

```http
POST /motion/clips/create-task
Content-Type: application/json
```

请求参数和 `/motion/clips/create` 相同。这个接口不会一直等素材生成完成，而是马上返回任务编号：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "task_id": "d7f0a1...",
    "task": {
      "status": "queued",
      "step": "queued",
      "message": "任务已创建，等待开始"
    }
  }
}
```

查询动作素材生成任务：

```http
GET /motion/tasks/{task_id}
```

返回示例：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "task_id": "d7f0a1...",
    "status": "running",
    "step": "build",
    "message": "正在截取视频、检测人脸并写入动作素材帧",
    "result": null,
    "error": ""
  }
}
```

`status` 常见值有 `queued`、`running`、`succeeded`、`failed`。任务成功后，`result` 里的结构和 `/motion/clips/create` 的返回内容一致。任务状态保存在后端内存里，重启服务后会清空。

选择动作素材：

```http
POST /motion/select
Content-Type: application/json
```

```json
{
  "sessionid": "123456",
  "kind": "speaking",
  "action_id": "lecture_explain"
}
```

`action_id` 支持：

| 取值 | 效果 |
| --- | --- |
| `lecture_explain` | 固定使用指定动作素材。 |
| `auto` / `pool` / `all` / `*` | 使用自动素材池。 |
| 空字符串 / `default` / `none` / `null` | 清空选择，回到默认素材。 |

修改素材元信息：

```http
POST /motion/clips/update
Content-Type: application/json
```

```json
{
  "sessionid": "123456",
  "kind": "speaking",
  "avatar_id": "teacher_avatar",
  "action_id": "lecture_explain",
  "next_action_id": "lecture_explain_01",
  "display_name": "普通讲解",
  "best_for": "讲解知识点",
  "tags": "speaking,teaching",
  "play_mode": "forward",
  "can_reverse": false,
  "weight": 3,
  "min_cycles": 1,
  "max_cycles": 1,
  "switch_at_boundary": true,
  "enabled": true
}
```

该接口只修改素材目录名和 `metadata.json`，不会重新生成帧。新版 avatar-local 素材还会同步更新 `motion.json` 中的素材索引。运行中的 session 会重新加载对应状态的素材。

删除素材：

```http
POST /motion/clips/delete
Content-Type: application/json
```

新版 avatar-local 素材删除后会同步从 `motion.json` 索引中移除。

```json
{
  "sessionid": "123456",
  "kind": "speaking",
  "avatar_id": "teacher_avatar",
  "action_id": "lecture_explain"
}
```

动作编排：

```http
POST /motion/plan
Content-Type: application/json
```

```json
{
  "sessionid": "123456",
  "avatar_id": "teacher_avatar",
  "text": "今天讲一元二次方程。",
  "max_segments": 6
}
```

如果配置了 `MOTION_LLM_API_KEY`、`MOTION_LLM_BASE_URL` 和 `MOTION_LLM_MODEL`，服务会调用大模型生成动作计划。如果没有配置，会使用规则兜底。

## 8. WebRTC 输出

普通 WebRTC：

```http
POST /offer
Content-Type: application/json
```

请求：

```json
{
  "sdp": "v=0...",
  "type": "offer",
  "avatar": "optional_avatar",
  "refaudio": "",
  "reftext": "",
  "custom_config": "",
  "max_width": 0,
  "max_height": 720,
  "fps": 15
}
```

`max_width`、`max_height`、`fps` 只对 packed alpha WebRTC 生效，用来降低远程透明显示的编码和网络压力。`0` 或不传表示不限制。`fps` 只有低于 avatar 原始 FPS 时才会抽帧，等于或高于原始 FPS 会按原始节奏输出。

返回：

```json
{"sdp": "v=0...", "type": "answer", "sessionid": "123456"}
```

packed alpha WebRTC：

```http
POST /alpha/webrtc/packed_offer
Content-Type: application/json
```

客户端 transceiver 顺序：

```text
audio recvonly
video recvonly
```

返回：

```json
{
  "sdp": "v=0...",
  "type": "answer",
  "sessionid": "123456",
  "tracks": {
    "audio": "audio",
    "packed": "video-0",
    "packing": "left=color,right=alpha"
  }
}
```

packed 视频一帧分左右两半：

```text
left  = color
right = alpha mask
```

例如原始 avatar 是 `1080x1920`，不限制时 packed 视频轨是 `2160x1920`；传 `max_height=720` 后，逻辑透明画面约为 `405x720`，packed 视频轨约为 `810x720`。

双轨 alpha WebRTC：

```http
POST /alpha/webrtc/offer
Content-Type: application/json
```

客户端 transceiver 顺序：

```text
audio recvonly
video recvonly  color
video recvonly  alpha mask
```

返回：

```json
{
  "sdp": "v=0...",
  "type": "answer",
  "sessionid": "123456",
  "tracks": {
    "audio": "audio",
    "color": "video-0",
    "alpha": "video-1"
  }
}
```

## 9. avatar 任务

创建：

```http
POST /api/avatar/task
Content-Type: application/json
```

```json
{
  "model": "wav2lip",
  "avatar_id": "my_avatar",
  "video_path": "/path/to/input.mp4",
  "img_size": 256,
  "pads": "0 10 0 0",
  "nosmooth": false,
  "face_det_batch_size": 16
}
```

上传视频时使用 `multipart/form-data`，文件字段为 `video_file`。

字段：

| 字段 | 说明 |
| --- | --- |
| `model` | 当前任务管理支持 `wav2lip` / `musetalk`。 |
| `avatar_id` | 输出目录名。 |
| `video_path` | 本地路径；相对路径会拼到 `./data/avatars/` 下。 |
| `video_file` | multipart 上传文件，保存到 `./data/tmp/`。 |
| `img_size` | Wav2Lip 裁剪尺寸，默认 `256`。 |
| `pads` | Wav2Lip 人脸框边距，顺序 `top bottom left right`，默认 `0 10 0 0`。 |
| `nosmooth` | Wav2Lip 是否关闭检测框平滑，默认 `false`。 |
| `face_det_batch_size` | Wav2Lip 人脸检测 batch，默认 `16`。 |
| `bbox_shift` | MuseTalk 人脸框偏移，默认 `0`。 |
| `extra_margin` | MuseTalk mask 额外边距，默认 `10`。 |
| `parsing_mode` | MuseTalk mask 模式，默认 `jaw`。 |
| `version` | MuseTalk 版本，默认 `v15`。 |
| `task_id` | 可选，自定义任务 ID。 |
| `notifyurl` | 可选，任务状态通知 URL。 |

返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "task_id": "task-id"
  }
}
```

查询：

```text
GET /api/avatar/task/{task_id}
GET /api/avatar/tasks
DELETE /api/avatar/task/{task_id}
```

任务状态：

```text
pending -> running -> completed / failed
```

只有 `pending` 状态任务可删除。

## 10. robottts 兼容 TTS

这是 `testclient/backend` 提供的测试 TTS 协议。生产接真实 `robot-tts` 时应保持同等兼容。

| 方法 | 路径 | 类型 | 作用 |
| --- | --- | --- | --- |
| `GET` | `/health` | JSON | 健康检查和音频格式。 |
| `GET` | `/tts/voices` | JSON | 音色列表。 |
| `POST` | `/tts` | JSON | 一次性合成 wav，测试用。 |
| `GET` | `/tts/ws` | WebSocket JSON + binary | LiveTalking 内部流式 TTS。 |
| `POST` | `/tts/task/create` | JSON | 创建外部推流任务。 |
| `POST` | `/tts/task/submit` | JSON | 提交任务文本并开始推流。 |
| `POST` | `/tts/task/start` | JSON | 创建并提交任务。 |
| `POST` | `/tts/task/cancel` | JSON | 取消任务。 |
| `GET` | `/tts/task/status?task_id=...` | JSON | 查询任务。 |

`GET /health`：

```json
{
  "status": "ok",
  "sample_rate": 16000,
  "channels": 1,
  "sample_width": 2,
  "format": "pcm",
  "provider": "edge"
}
```

当 `TEST_TTS_PROVIDER=bailian` 时，额外返回 `model` 字段（如 `"model": "cosyvoice-v3-flash"`）。

`GET /tts/voices`：

```json
{
  "voices": [
    {"id": 0, "name": "zh-CN-XiaoxiaoNeural", "description": "zh-CN-XiaoxiaoNeural"}
  ]
}
```

`POST /tts`：

```json
{
  "text": "你好",
  "voice_id": 0,
  "mode": "instruct2",
  "prompts": "请自然清晰地朗读。",
  "output_path": "/tmp/robottts-output.wav"
}
```

返回：

```json
{
  "success": true,
  "audio_path": "/tmp/robottts-output.wav",
  "duration": 1.23
}
```

`WS /tts/ws` 流程：

```text
LiveTalking -> {"action":"start","voice_id":0,"mode":"instruct2","prompts":"..."}
TTS -> {"action":"started"}
LiveTalking -> {"action":"text","text":"你好"}
TTS -> binary PCM16 chunks
TTS -> {"action":"result","type":"final","meta":{...}}
LiveTalking -> {"action":"end"}
```

`POST /tts/task/create`：

```json
{
  "task_id": "demo-001",
  "voice_id": 0,
  "mode": "instruct2",
  "prompts": "请自然清晰地朗读。",
  "target_hardware": "ws://127.0.0.1:8050/alpha/input/audio"
}
```

`POST /tts/task/submit`：

```json
{
  "task_id": "demo-001",
  "text": "这段话由 TTS 生成音频，再推给 LiveTalking。"
}
```

`POST /tts/task/start` 等价于 create + submit：

```json
{
  "task_id": "demo-001",
  "text": "这段话由 TTS 生成音频，再推给 LiveTalking。",
  "voice_id": 0,
  "mode": "instruct2",
  "prompts": "请自然清晰地朗读。",
  "target_hardware": "ws://127.0.0.1:8050/alpha/input/audio"
}
```

返回：

```json
{"success": true, "task_id": "demo-001", "state": "submitted"}
```

查询：

```text
GET /tts/task/status?task_id=demo-001
```

返回：

```json
{"task_id": "demo-001", "state": "done", "owns_active_slot": false}
```

取消：

```http
POST /tts/task/cancel
Content-Type: application/json
```

```json
{"task_id": "demo-001"}
```

## 11. 代码位置

| 功能 | 文件 |
| --- | --- |
| 主服务启动 | `app.py` |
| HTTP/alpha 路由 | `server/routes.py` |
| avatar 任务路由 | `server/avatar_routes.py` |
| avatar 任务执行 | `server/task_manager.py` |
| alpha WebSocket 输出 | `server/alpha_stream.py` |
| WebRTC | `server/rtc_manager.py`、`server/alpha_webrtc.py` |
| session 管理 | `server/session_manager.py` |
| robottts 插件 | `tts/robottts.py` |
| 测试 TTS | `testclient/backend/robottts_test_server.py` |
| Web 测试页 | `testclient/web/src/main.jsx` |
| Electron overlay | `testclient/overlay/` |

此外，`/` 路径会 serve `web/` 目录下的静态文件（`index.html`、`dashboard.html` 等前端测试页面）。
