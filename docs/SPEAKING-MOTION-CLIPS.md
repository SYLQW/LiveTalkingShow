# Speaking Motion Clips

这份说明记录当前新增的“说话状态动作片段”能力。它的目的不是替代 `audiotype`，而是在 `audiotype=0` 正常说话的时候，选择不同的身体动作片段作为 Wav2Lip 的底图。

## 目录结构

说话动作片段放在：

```text
data/speaking_actions/<avatar_id>/<action_id>/
  full_imgs/
  face_imgs/
  coords.pkl
  metadata.json
  preview.png
```

每个 `<action_id>` 都是一段可用于说话状态的动作素材，例如 `lecture_explain`、`lecture_emphasize`、`lecture_point`。

静息动作片段放在：

```text
data/idle_actions/<avatar_id>/<action_id>/
  full_imgs/
  face_imgs/
  coords.pkl
  metadata.json
  preview.png
```

静息动作只在数字人不说话的时候播放。没有选择静息动作时，系统默认固定使用 avatar 的第一帧，避免空闲时也一直播放讲解动作。

## 生成动作片段

可以用命令行生成：

```powershell
python tools\build_speaking_motion_clip.py `
  --source "G:\数字人\数字人原型\视频素材\生成哑巴老师上课视频-优秀，但是动作不行.mp4" `
  --avatar-id mute_teacher_motion_v1_pad01000 `
  --action-id lecture_explain `
  --display-name 普通讲解 `
  --out-root data\speaking_actions `
  --start 0 `
  --fps 30 `
  --img-size 256 `
  --pads 0 10 0 0 `
  --face-det-batch-size 8 `
  --use-ffmpeg-cut `
  --ffmpeg-path "G:\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe" `
  --chroma-key `
  --overwrite
```

常用参数：

```text
--source
源视频或图片目录。

--avatar-id
动作片段归属的 avatar。运行时只会加载当前 avatar_id 下面的动作片段。

--action-id
动作编号，建议使用英文和下划线，例如 lecture_explain。

--start / --end
截取视频片段的开始和结束秒数。

--fps
生成动作片段使用的帧率。

--pads
人脸框调整参数，顺序是 top bottom left right。

--chroma-key
源视频是绿幕时使用，生成透明背景。

--use-ffmpeg-cut
先用 FFmpeg 按 start/end 截出一段临时视频，再进入拆帧和人脸检测。源视频较长，或者只想取中间一段动作时建议打开。

--ffmpeg-path
FFmpeg 可执行文件路径。当前本机路径是 G:\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe。

--max-frames
快速测试时可以限制帧数，正式生成时设为 0 或不填。
```

默认生成帧率现在按 `30fps` 处理。这样动作会更流畅一些，也更接近原始视频素材的观感，不过生成出来的帧数会更多，处理时间和素材占用也会变大。要是只是快速试参数，可以临时把 fps 调低；要是准备放进素材库正式使用，建议先用 30。

## 后端接口

读取本地视频信息，给制作页显示视频时长、帧率、分辨率：

```http
POST /motion/source/probe
```

请求体：

```json
{
  "source": "G:/数字人/数字人原型/视频素材/生成哑巴老师上课视频-优秀，但是动作不行.mp4",
  "ffmpeg_path": "G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffmpeg.exe"
}
```

接口会返回 `video_url`，制作页会用这个地址预览本地视频。浏览器不能直接播放后端机器上的 `G:\...` 路径，所以这里需要先让后端把这个视频文件提供出来。

预览本地视频：

```http
GET /motion/source/video?source=<本地视频路径>
```

查看当前 session 已加载的动作片段：

```http
GET /motion/clips?sessionid=<sessionid>&kind=speaking
```

查看静息动作片段：

```http
GET /motion/clips?sessionid=<sessionid>&kind=idle
```

选择说话或者静息时使用的动作片段：

```http
POST /motion/select
```

请求体：

```json
{
  "sessionid": "当前 session id",
  "kind": "speaking",
  "action_id": "lecture_explain"
}
```

清空当前说话动作选择，回到默认素材：

```json
{
  "sessionid": "当前 session id",
  "kind": "speaking",
  "action_id": ""
}
```

清空当前静息动作选择时，`kind` 改成 `idle`，系统会回到固定第一帧。

通过接口生成动作片段：

```http
POST /motion/clips/create
```

请求体示例：

```json
{
  "sessionid": "当前 session id",
  "kind": "speaking",
  "source": "G:/数字人/数字人原型/视频素材/生成哑巴老师上课视频-优秀，但是动作不行.mp4",
  "action_id": "lecture_explain",
  "display_name": "普通讲解",
  "start": 0,
  "fps": 30,
  "max_frames": 0,
  "img_size": 256,
  "pads": [0, 10, 0, 0],
  "face_det_batch_size": 8,
  "chroma_key": true,
  "use_ffmpeg_cut": true,
  "ffmpeg_path": "G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffmpeg.exe",
  "overwrite": false
}
```

接口创建完成后，会刷新当前 session 的动作片段列表。

如果同名 `action_id` 已经存在，并且 `overwrite` 是 `false`，后端会拒绝生成。前端默认也是不覆盖已有素材，避免误把已经调好的动作片段替换掉。

如果要生成静息动作，把 `kind` 改成 `idle`。静息动作建议选择幅度小的片段，例如轻微呼吸、自然站立、等待学生回答、低幅度点头等，不建议使用大幅挥手或者指向动作。

修改动作片段说明信息：

```http
POST /motion/clips/update
```

请求体示例：

```json
{
  "sessionid": "当前 session id",
  "avatar_id": "mute_teacher_motion_v1_pad01000",
  "kind": "speaking",
  "action_id": "lecture_explain",
  "next_action_id": "lecture_explain",
  "display_name": "普通讲解",
  "description": "适合普通知识点讲解，动作幅度不大，可以长时间使用。",
  "best_for": "概念解释、例题讲解",
  "tags": "speaking,teaching,explain"
}
```

`action_id` 是当前素材编号，`next_action_id` 是保存后的素材编号。如果两者不同，后端会把素材目录改名。这个接口只修改 `metadata.json` 和目录名，不会重新生成视频帧。

## 前端页面

主测试页只保留“说话动作”面板，用来刷新动作列表和选择当前说话状态使用的动作片段。制作动作片段已经拆到单独页面：

```text
http://127.0.0.1:8070/motion.html
```

动作片段制作页可以做这些事：

```text
选择片段类型：说话动作或者静息动作。
填写源视频路径，然后加载视频预览。
拖动视频进度条，选择合适的动作起点和终点。
把当前开始点和结束点保存成一个片段，并且给它起一个 action_id，例如 lecture_explain、lecture_emphasize。
把多个片段加入待生成队列，逐个生成或者全部生成。
生成完成后，片段会进入可用素材库，后面主测试页就能选择这些动作片段。
```

`fps`、`img_size`、`pads`、人脸检测批量、绿幕扣除和 FFmpeg 路径放在“生成参数”里。一般操作的时候先不需要动它们，只有嘴部位置、清晰度或者截取方式不对的时候再调。

源视频现在有两种给法：可以手动填写后端机器上的本地视频路径，也可以点“选择视频”把文件上传到后端临时目录。上传后的路径会自动填回页面，并且会继续走加载视频、截取片段、生成动作素材这套流程。

页面里还有“检查人脸框”。点它以后，会截取当前片段开始点附近的一帧来显示人脸范围。蓝色虚线框是检测器找到的原始人脸框，红色框是加了 `pads` 以后真正拿去生成 `face_imgs` 和 `coords.pkl` 的生成框。拖动上、下、左、右四个滑块，只是在这个检测框基础上调整生成框，方便看清楚 Wav2Lip 到底会截哪一块脸。

主测试页里有两组选择：说话动作和静息动作。说话动作只在有语音的时候生效；静息动作只在没有语音的时候生效。

## 运行逻辑

说话动作片段只在 `audiotype=0` 的时候生效。后端仍然把当前 TTS 音频送进 Wav2Lip，动作片段只改变 Wav2Lip 使用的 `full_imgs`、`face_imgs` 和 `coords.pkl`。

`audiotype=2`、`audiotype=3` 这类自定义动作仍然适合做不说话的短动作，或者固定话术短片。它们不是这次“边说边动”的主要方式。

## Wav2Lip 模型替换评估

当前主线仍然建议先保留 256 版 Wav2Lip。它的效果不算最清晰，但已经能和现有的动作素材、透明背景、前端贴回方式配合起来，调试成本比较低。

`Wav2Lip_Chinese` 可以先作为小范围测试项。如果它的模型结构和当前 Wav2Lip 一致，那大概率可以通过替换模型文件，或者把 `LIVETALKING_MODELFILE` 指到新的权重来试。它的重点更偏中文口型，可能会让中文发音的嘴型更贴一些，但不一定能明显解决“画面糊”的问题。测试的时候要用同一段视频、同一段 TTS、同一组 `pads` 和同一个 `img_size`，这样才能看出差别。

`Wav2Lip-HD` 对项目影响会更大。很多 HD 方案不是单纯换一个权重，而是把 Wav2Lip、脸部增强、超分或者后处理放在一起跑。这样画质可能更好，但会增加处理时间，也可能需要改推理代码、模型加载方式和结果贴回方式。它更适合先单独开一个实验分支，把离线生成效果跑出来，再决定要不要接回现在的实时页面。

这里也要注意，`pads` 和 `face_det_batch_size` 不是直接提高画质的参数。`pads` 主要决定人脸裁剪范围，框太偏会让嘴跑位，框太大可能让嘴部细节变弱；`face_det_batch_size` 主要影响人脸检测一次处理多少帧，默认改成 8 是为了让检测效率更好一些，但它不会把嘴唇本身变清晰。

## 当前限制

动作片段里的人脸不能大幅侧转，也不要被手遮挡。

源视频最好是闭嘴或轻微张嘴。如果原视频本身有明显说话口型，重新套当前 TTS 嘴型时效果会差。

动作切换建议放在句子或短段落边界，不要一秒内切很多次。

前端现在只做路径输入和参数控制，视频处理仍然由后端完成。
