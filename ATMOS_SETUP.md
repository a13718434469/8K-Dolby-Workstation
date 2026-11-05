# Atmos 编码配置与双杜比验收（2.4）

2.4 的默认目标是 **Dolby Vision 8.4 + E-AC-3 JOC / Dolby Atmos**。已有合规 JOC 音轨可直接保留；普通音轨通过空间 PCM 母版和真实编码后端制作。缺少编码条件或输出检测不符时任务失败，不会悄悄改成 AAC、普通 E-AC-3 或静音。

**当前交付边界：**母版制作、DEE 命令适配、AWS API 接入、严格检测和恢复保护已实现并通过本地回归。本轮开发环境未配置可运行的授权 DEE 或 AWS 编码账户，所以没有实际运行“普通 PCM → 新的真正 JOC”，也没有完成 B站双标验收。没有上传媒体或产生云端费用。预制 JOC 夹具和模拟 AWS 测试不替代这两项实测。

## 选择编码方式

| 方式 | 使用条件 | 工作站行为 |
| --- | --- | --- |
| 保留已有 Atmos | 第一条音轨已经是受支持的 E-AC-3 JOC | 原码流复制；不要求本地 DEE、AWS 账户或新编码费用 |
| 本地 DEE | 完整 Dolby Encoding Engine、有效授权、该安装版本可用的 5.1.4 PCM → JOC 命令 | 本地制作母版，运行配置的命令并严格检查结果 |
| AWS MediaConvert | AWS 账户、区域、私有 S3、服务角色、可用凭证及本次上传/计费同意 | 上传 PCM 母版、请求 JOC 编码、下载、检查并封装；画面在本地制作 |

`setup.bat` 安装 Python 依赖（含 `boto3`）、FFmpeg 和 dovi_tool 等基础工具，不安装 Dolby 商业许可证，不创建云账户或云资源。安装第三方编码器与获得其使用授权是独立步骤。[Dolby Encoding Engine 官方入口](https://professionalsupport.dolby.com/s/dolby-encoding-engine?language=en_US)

## 普通音轨会怎样处理

程序使用视频或外部文件的第一条音轨。当前支持声道顺序明确的 mono、stereo、5.1、5.1(side) 和 7.1；未知多声道顺序会停止处理。TrueHD 等素材可先解码到这些声道，但此流程不会保留其原有 Atmos 对象元数据。

空间母版固定为 48kHz、24-bit 整数 PCM：本地后端使用 10 声道 5.1.4，AWS 使用 16 声道 9.1.6。立体声保留左右前方声像，使用较轻的差分环境声补充空间；多声道保留主要声道关系；单声道放在中置，其他方向不凭空制造声音。高度环境声采用受限频段和轻微延时。这是一次新的混音创作，不代表恢复了录制时不存在的声音对象、高度或运动。

同一视频容器内按音频与画面的起点差对齐；独立替换音频从视频零点开始配对。短素材补静音，长素材按视频目标长度截取。若原文件使用复杂剪辑列表、人工偏移或变速，应先测试短片口型、打击点和首尾同步。PCM 母版比压缩音轨大：5.1.4 约 86MB/分钟，9.1.6 约 138MB/分钟，需要相应的本地空间及云上传带宽。

## 本地 DEE 设置

1. 从 Dolby 提供的渠道取得完整 DEE，完成其许可证安装。使用安装版本自带的官方帮助和示例，先确认能把 **5.1.4 交错 PCM WAV** 编码为 E-AC-3 JOC。
2. 打开工作站的“Atmos 编码设置”，选择“本地 Dolby Encoding Engine”，指定编码程序与下面说明的 JSON 配置文件。未指定程序路径时，只从项目工具目录与 `PATH` 查找 `dee_ddpjoc_encoder`。
3. 编码配置就绪仅代表路径和参数结构有效。实际运行仍须通过许可证检查及真正 JOC 检测。不要把 Renderer 内置组件、普通 FFmpeg E-AC-3 编码器或一个同名可执行文件当成已授权 DEE。

### 编码配置文件结构

JSON 对应**本机安装版本已验证可用的命令**，不是许可证。程序不猜测版本间不同的参数，也不生成旧版 XML。配置文件最大 64KB，只接受下列字段：

| 字段 | 内容 |
| --- | --- |
| `schema_version` | 必须为整数 `1` |
| `input_layout` | 必须为 `"5.1.4"`，表示接受 10 声道交错 PCM WAV |
| `argv` | 命令参数的字符串数组，不包含可执行文件本身；须从该版本官方命令复制 |
| `output_suffix` | `".ec3"`、`".eac3"` 或 `".mp4"`；省略时为 `.ec3` |
| `description` | 可选说明，例如安装版本及配置用途；不要放账号或密钥 |

`argv` 必须分别包含 `{input}` 和 `{output}`，可选 `{temp}`。程序把它们替换为母版文件、期望输出文件和任务临时目录的绝对路径，并直接传给进程；每个参数是单独一个数组元素，不使用 shell，也不把命令行整句拆词。不要在 JSON 中填写密钥、许可证正文或其他秘密信息。

把配置保存在自己的编码器配置目录，推荐文件名以 `.dee-profile.json` 结尾。该后缀已被仓库忽略；不要把个人配置、编码器或许可证加入源码包。

本地母版通道顺序为 `L R C LFE Ls Rs Ltf Rtf Ltr Rtr`。**普通 PCM WAV 不是 ADM BWF。**如果安装版本只接受 ADM / DAMF，本适配不能把文件改名后当成相应母版；需要该版本支持的 5.1.4 CBI 编码入口，或使用 AWS 路线。这里不提供未经该版本实际验证的 DEE 参数样例。

下面是工作站自身的 CLI 参数示例。两个路径必须换成已经安装和验证的文件：

```powershell
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "dual.mp4" --auto-double-dolby --atmos-backend dee --dee-path "C:\Dolby\dee_ddpjoc_encoder.exe" --dee-profile "C:\Dolby\workstation.dee-profile.json"
```

## AWS MediaConvert 设置

AWS 官方支持由 CBI、ADM BWF 或 DAMF 制作 Dolby Digital Plus JOC。工作站当前选择固定通道顺序的 16 声道 CBI 输入，请求 `EAC3_ATMOS`、48kHz、768kbps 和 `CODING_MODE_9_1_6`。[AWS Atmos 配置说明](https://docs.aws.amazon.com/mediaconvert/latest/ug/using-dolby-atmos-encoding.html)

当前接入使用 **音频专用 DASH 单文件输出**，下载其中唯一的 MP4 音频文件，再在本地无损封装到 DV 画面。这样对应 AWS 音频专用输出表中明确列出的 JOC 组合；不会把“普通 MP4 + EAC3”的接口参数当作 Atmos 支持。[AWS 音频专用输出格式表](https://docs.aws.amazon.com/mediaconvert/latest/ug/audio-only-output.html)

### 先准备账户与权限

- 选择可使用 MediaConvert 的 AWS 区域，准备输入与输出使用的**私有** S3 存储桶。工作站不会替你关闭桶的公开访问，桶名称和访问策略需事先正确设置。
- 创建 MediaConvert 服务角色，允许服务读取本次输入并写入本次输出。角色 ARN 与运行工作站的登录凭证不是同一项；使用 KMS 加密时还需相应密钥权限。[AWS 服务角色设置](https://docs.aws.amazon.com/mediaconvert/latest/ug/iam-role.html)
- 本机调用身份需能够创建、查询和取消 MediaConvert 任务，传递指定服务角色，并上传、读取、列出及删除任务目录内的 S3 对象。启用 S3 版本控制时，清理当前对象版本还需要相应版本删除权限。应把权限限定在实际使用的角色、桶和前缀；以 AWS 权限配置与服务返回结果为准。
- 按 AWS 官方方法配置本机凭证链，例如配置名或可刷新的临时身份。工作站只接收配置名，不提供访问密钥/秘密密钥输入框，不把凭证写入任务参数。[Boto3 凭证配置](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)

### 填写界面

选择“Atmos 编码设置 → AWS MediaConvert 云端编码”，填写：

| 界面字段 | 填写方式 |
| --- | --- |
| AWS 区域 | 实际使用的区域，例如 `ap-southeast-1`；不保证所有账户/区域均已开通 |
| S3 存储桶 | 桶名本身，例如 `your-private-audio-bucket`，不加 `s3://` 或子目录 |
| MediaConvert 角色 | 对应服务角色的完整 ARN |
| 本机 AWS 配置名 | 已配置的本机名称；留空使用 SDK 默认凭证链 |
| 临时目录前缀 | 默认 `dolby-workstation`，每个任务会创建独立随机子目录 |
| 本次允许上传并计费 | 阅读提示后主动勾选；关闭重开应用后不会自动恢复此同意 |

云端编码会上传音频 PCM 母版，产生 MediaConvert 编码、S3 存储以及可能的传输费用。程序不会在显示配置状态时查询云端或提交任务；真正开始处理后才使用系统凭证访问 AWS。界面显示“配置就绪”不证明账户权限、余额、区域配额或服务端编码已经实测可用。

CLI 同样需要明确的单次参数。下例中的桶、账号、角色和配置名均须替换为自己的实际值；只有决定执行本次云任务时才传入 `--allow-cloud-upload`：

```powershell
.\.venv\Scripts\python.exe cli.py -i "source.mp4" -o "dual.mp4" --auto-double-dolby --atmos-backend aws --aws-region "ap-southeast-1" --aws-bucket "your-private-audio-bucket" --aws-role-arn "arn:aws:iam::123456789012:role/YourMediaConvertRole" --aws-profile "workstation" --aws-prefix "dolby-workstation" --allow-cloud-upload

# 恢复原任务；只读取原参数，不允许借恢复命令更换目标或后端
.\.venv\Scripts\python.exe cli.py --resume --workspace "workspace" --allow-cloud-upload
```

### 取消、异常与重复计费保护

云任务在提交前写入本地 `downloaded.mp4.cloud-job.json`，随后保存请求标识、已确认的任务编号、状态和下载哈希。恢复时只复用与母版和配置一致、文件大小及 SHA256 均匹配的完整下载。

如果提交响应不确定、任务仍未确认终止、记录损坏、下载缺失或内容改变，程序会停止，**不会自动创建另一项收费编码**。先在 AWS 查看日志中的任务编号或请求标识，确认原任务和费用；需要重新制作时，再明确新建任务。不要删除本地记录来绕过保护。

停止操作会请求取消已经取得编号的云任务，并尝试只清理本任务随机目录内的临时对象。网络或权限错误可能使取消、清理无法确认，程序会给出任务目录提示，应在 AWS 再核对。程序不会删除桶或其他任务目录，也不会保证取消前已经发生的费用被撤销。

## 必须完成的真实验收

1. 使用自己有权处理的 10–15 秒普通立体声音频和短视频，先确认口型、声道和首尾位置。选择默认双杜比目标以及实际可用的 DEE 或 AWS 后端运行。
2. 查看成片旁的 `*.quality.json`：检查实际后端、普通素材生成分支、真实 JOC 证据、5.1/7.1 交付声道、48kHz、至少 320kbps、完整解码和最终音频负载一致性。生成音轨与目标视频时长允许的差异不超过 80ms。不能拿预制夹具适配报告代替这次真实编码。
3. 在支持 HDR / Atmos 的回放链上检查画面高光、暗部、色阶、声音空间感、对白位置和音画同步。单声道测试不应被描述为已经恢复了立体或移动对象。
4. 由用户在 **B站 WEB 创作中心**上传这个短测试片，勾选 **高级设置 → 杜比音效**。官方帮助要求符合 E-AC-3 Atmos、5.1/7.1、相应采样率与码率，并说明全景声在**开放浏览后**出现；只有草稿未显示标识不能判定失败。[官方 Atmos 帮助](https://www.bilibili.com/blackboard/help.html#/?qid=300&pid=297)（条目 3792 / 3794；[官方公开数据](https://api.bilibili.com/x/web-goblin/help/detail?questionTypeId=300&ps=20&version=1)）
5. 等待目标画质及音频转码完成，在支持设备与相应播放权限下确认杜比视界、杜比全景声两个选项及实际回放。保存源文件 SHA256、程序/编码器版本、报告、稿件编号和实际播放证据。工作站不会自动发布，也不会自行把 `platform_verified` 改成 `true`。

画面侧仍参考 B站公开的 HLG、BT.2020、10-bit HEVC、Profile 8.4 及 MP4/MOV/M4V 要求。2021 年公告中的最高 4K60 与约 30Mbps 建议是旧版参考，不代表当前完整规则。[B站杜比视界官方公告](https://www.bilibili.com/opus/560508706903943321)

## 本地测试覆盖什么

`tests/test_atmos_encoding.py` 检查实际 PCM 声道、时长、混音及配置边界；`tests/test_atmos_cloud.py` 用官方 SDK 请求模型与模拟服务验证配置、上传下载、取消和异常恢复；`tests/test_atmos_pipeline.py` 使用实际 FFmpeg 验证普通 E-AC-3 被拒、已有 JOC 保留、预制夹具适配、恢复缓存和最终码流一致性。

这些自动测试不会申请 Dolby 许可证、调用真实 AWS 编码或上传 B站。只有本节前面的真实编码、监听和平台验收完成后，才能据实说明某个部署环境已经获得双标；代码与本地测试通过本身不构成平台保证。
