# DICOM 批量匿名化 API

基于 **FastAPI + pydicom + Docker** 的 DICOM 影像批量匿名化服务。

上传一个包含 DICOM 文件的 ZIP，服务会：

1. 用**用户提供的稳定密钥**（stable secret）对患者身份做 HMAC-SHA256 派生，生成
   **患者级稳定伪名**（同一患者跨批次伪名相同；换密钥则全部不同）。身份键优先由
   `PatientID` 与其签发机构 `IssuerOfPatientID` 共同确定——不同机构复用同一患者号
   不会被合并，签发机构为空时按患者号单独确定（详见
   [患者身份归组规则](#患者身份归组规则patient_key)）；
2. 重写 `PatientID` / `PatientName` 等身份字段，清空/删除 DICOM PS3.15 Annex E
   Basic Application Confidentiality Profile 要求处理的机构、人员、自由文本标签及
   **全部私有标签**；
3. 对 `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID` /
   `FrameOfReferenceUID` 以及**序列内任意深度的 UID 引用**使用一张全局映射表做
   一致性重写（同一原始 UID 永远映射到同一新 UID），从而**保持检查/系列/实例/
   RT 引用关系不变**；
4. （可选，默认开启）对每位患者在**可配置闭区间**（`date_shift_min`..`date_shift_max`，
   默认 -365..365 天）内派生一个稳定的日期偏移，平移全部 `DA`/`DT` 标签，
   患者出生与检查日期使用同一偏移；同一患者跨批次且范围相同时偏移一致。
   `DA`/`DT` 的**多值**（pydicom 多值及反斜杠分隔值）按同一项数与顺序**逐项**
   平移，`DT` 仅替换日期前缀、保留时间/时区尾部；任一项日期非法时按
   `INVALID_DATE` 规则清空整个标签（不残留合法项）；
5. 返回**标签变更预览**（逐标签 old → new）与**逐文件诊断**（非法 DICOM、
   引用缺失、UID 缺失等），并提供**匿名化 ZIP 下载**（扁平文件名，不保留原始
   目录结构，避免路径泄露 PHI）。

> ⚠️ 请妥善保管稳定密钥。服务不存储密钥，只在响应中返回其 SHA-256 指纹前 12 位
> 以便核对批次；密钥一旦丢失，无法将新旧伪名/UID 对应回原值。

---

## 一键启动

需要安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build -d
```

启动后服务监听：

| 项目 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 交互式文档 (Swagger UI) | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |
| 健康检查 | <http://localhost:8000/health> |

端口可通过环境变量修改，例如改用 9000 端口：

```bash
DICOM_ANON_PORT=9000 docker compose up --build -d
# API: http://localhost:9000
```

停止：

```bash
docker compose down
```

不使用容器本地运行（Python 3.11）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 任务持久化与重启恢复

任务数据保存在工作目录（容器内 `/data/dicom-anon`，Compose 命名卷
`dicom-anon-data`）中。**重启容器 / 重新部署后，未过期的任务仍可继续查询、
下载与使用各子资源，无需重新匿名化**：

1. 处理完成后，服务把**任务元数据、逐文件预览与诊断、结果索引**写入任务目录
   下的版本化清单 `manifest.json`（先写临时文件再原子替换，崩溃不会留下
   半个清单）；清单只保存密钥的 SHA-256 指纹前 12 位，
   **绝不保存稳定密钥本身**；
2. 启动时扫描工作卷，仅恢复同时满足以下条件的任务：
   **未过期**、**清单完整**（版本匹配、字段齐全）、**输出文件与索引一致**
   （文件名集合、大小与 SHA-256 逐项匹配）；过期或中断残留的目录、
   损坏/版本不符的清单、被篡改或缺失的输出，以及一次性临时 ZIP
   一律在启动时清理；
3. 恢复后原任务 ID 的查询（`GET /jobs/{id}`、`/files`）、各子资源
   （检查子集 / DICOMDIR / 系列几何 / KVP / PNG 预览 / 剂量事件 /
   SEG 覆盖率）与下载（全量 / 跨任务合并）照常可用；
4. 到期时间随清单持久化（`created_at + DICOM_ANON_JOB_TTL_SECONDS`），
   **运行中清理与重启恢复使用同一到期时间**；之后修改 TTL 配置只影响
   新创建的任务。

卷配置（`docker-compose.yml`，默认命名卷，首次启动自动初始化）：

```yaml
volumes:
  - dicom-anon-data:/data/dicom-anon   # 命名卷：任务目录、输出与清单
```

- 跨重启保留任务：保留命名卷即可（`docker compose down` 不会删卷）；
- 清空全部任务数据：`docker compose down -v`（连卷一起删除）；
- 改用宿主机目录：把卷映射改为 `./data:/data/dicom-anon` 之类的绑定挂载。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/jobs` | 上传 ZIP（multipart），同步处理并返回完整标签变更预览与诊断 |
| `POST` | `/jobs/consistency-check` | 核对 2～5 个已完成任务的密钥指纹、患者伪名与 Study/Series/SOP UID 映射；支持差异明细分页（`level`/`limit`/`cursor`） |
| `POST` | `/jobs/merged-study/download` | 跨 2～5 个**同密钥**任务合并下载某个匿名检查：递归补全跨任务 SOP 引用，按匿名 SOPInstanceUID 字节去重，冲突/缺失返回 `409` |
| `GET` | `/jobs/{job_id}` | 获取任务结果（预览 + 诊断 + UID 映射概览） |
| `GET` | `/jobs/{job_id}/files` | 仅获取逐文件诊断与变更列表 |
| `GET` | `/jobs/{job_id}/download` | 下载匿名化后的 ZIP（全量） |
| `GET` | `/jobs/{job_id}/dicomdir/download` | 下载带 **DICOMDIR** 的离线阅片 ZIP：按匿名伪名 / Study/Series/SOP UID 重建四级目录层级，实例映射为合规稳定短 File ID |
| `GET` | `/jobs/{job_id}/studies/{study_instance_uid}/download` | 按**匿名 StudyInstanceUID** 下载检查子集（递归补全 SOP 引用闭包，含跨检查引用） |
| `GET` | `/jobs/{job_id}/series/{series_instance_uid}/geometry` | 按**匿名 SeriesInstanceUID** 分析单帧切片几何：方向余弦叉积法线、投影排序、中位数层间距与 `>1.5×` 异常缺口标记，冲突时返回含匿名 SOP UID 的明细 |
| `GET` | `/jobs/{job_id}/series/{series_instance_uid}/kvp` | 按**匿名 SeriesInstanceUID** 统计 CT 管电压（KVP）逐帧分布：各电压帧数、缺失帧数与按匿名 SOP UID 排列的逐实例明细，便于发现同系列混用扫描参数 |
| `GET` | `/jobs/{job_id}/instances/{sop_instance_uid}/preview.png` | 按**匿名 SOPInstanceUID** 返回实例影像的 8 位灰度 **PNG 预览**（未压缩、单帧、8/16 位 MONOCHROME1/2），供人工核对影像内容 |
| `GET` | `/jobs/{job_id}/instances/{sop_instance_uid}/dose-events` | 按**匿名 SOPInstanceUID** 解析 **X-Ray Radiation Dose SR**：递归遍历 ContentSequence，按 DCM 编码的 CT 照射事件容器提取逐事件 CTDIvol（mGy）/ DLP（mGy·cm）并给出事件 DLP 合计；事件数据缺项/重复/非法/单位不符返回 `409` |
| `GET` | `/jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage` | 按**匿名 SOPInstanceUID** 统计**未压缩 BINARY SEG** 的分割覆盖率：逐帧用 `ReferencedSegmentNumber` 与**全部** `DerivationImageSequence/SourceImageSequence` 的匿名引用（按匿名 SOP UID 去重）关联任务内唯一源图像，同（源图像, 段号）多帧 1-bit 掩码逐像素并集，返回前景像素数/图像像素数/覆盖率；引用缺失、**同一帧引用多个不同源 UID**、引用歧义、尺寸不一致、帧数或像素数据异常返回 `409` |
| `GET` | `/health` | 健康检查 |

### 表单参数（`POST /jobs`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | `.zip` 压缩包（默认上限 512 MiB） |
| `secret` | string | 是 | 稳定密钥，至少 8 个字符 |
| `shift_dates` | boolean | 否 | 是否平移日期，默认 `true` |
| `date_shift_min` | integer | 否 | 日期平移闭区间下界（天），默认 `-365`；须与 `date_shift_max` **成对提供** |
| `date_shift_max` | integer | 否 | 日期平移闭区间上界（天），默认 `365`；须与 `date_shift_min` **成对提供** |

日期平移范围规则：

- 两者必须**同时给出或同时省略**；只给一个返回 `422`。
- 必须满足 **`-365 ≤ date_shift_min ≤ date_shift_max ≤ 365`**，越界或下界大于上界
  返回 `422`（非整数同样 `422`）。
- 偏移由 `HMAC-SHA256(secret, "date-shift", 患者身份, 区间)` 确定性地在闭区间内均匀
  选取，**同一患者跨批次且范围相同时偏移完全一致**；范围本身参与派生，因此换范围即
  换偏移（用于让不同处理策略批次可区分）。默认区间的派生与升级前版本逐字节兼容。
- 当 `shift_dates=false` 时范围参数会被接受但不起作用，每位患者的偏移固定为 `0`，
  `DA`/`DT` 不平移、`PatientBirthDate` 清空。
- 零宽区间可强制固定偏移，例如 `date_shift_min=date_shift_max=0` 表示“保留原日期”。

### curl 示例

```bash
# 1) 上传并创建任务（响应即标签变更预览）
curl -X POST http://localhost:8000/jobs \
  -F "file=@studies.zip;type=application/zip" \
  -F "secret=my-stable-secret-2026" \
  -F "shift_dates=true" \
  -o job.json

# 将日期平移范围限定为 ±30 天（两个字段必须成对出现）
curl -X POST http://localhost:8000/jobs \
  -F "file=@studies.zip;type=application/zip" \
  -F "secret=my-stable-secret-2026" \
  -F "shift_dates=true" \
  -F "date_shift_min=-30" \
  -F "date_shift_max=30" \
  -o job.json

JOB_ID=$(python3 -c "import json;print(json.load(open('job.json'))['id'])")

# 2) 查看逐文件诊断 / 变更预览
curl http://localhost:8000/jobs/$JOB_ID/files

# 3) 下载匿名化 ZIP（文件名为新 SOPInstanceUID，扁平结构）
curl -OJ http://localhost:8000/jobs/$JOB_ID/download
```

### 患者身份归组规则（`patient_key`）

> API 默认地址：<http://localhost:8000>（Swagger 文档 <http://localhost:8000/docs>，
> 健康检查 <http://localhost:8000/health>）。

服务两遍扫描 ZIP 内全部文件，先为每个文件计算**患者身份键（`patient_key`）**：
身份键相同的文件归为同一患者，共用患者级稳定伪名与日期偏移。身份键贯穿三处口径，
保证互相对齐：

- 逐文件结果 `files[].patient_key`；
- 患者汇总 `patients[].key`（`POST http://localhost:8000/jobs` 与
  `GET http://localhost:8000/jobs/{job_id}`）；
- 跨任务一致性核对 `POST http://localhost:8000/jobs/consistency-check`。

归组优先级如下：

1. **非空 `PatientID` 且 `IssuerOfPatientID`（患者号签发机构，(0010,0021)）非空**：
   以签发机构与患者号**共同**确定身份键，形如 `id:<签发机构>␟<患者号>`
   （`␟` 为 ASCII 单元分隔符 `0x1F`；该字符不可能出现在合法 LO 字段中，因此
   机构号与患者号的任意取值都不会产生歧义键）。两家签发机构在同一 ZIP 或不同批次中
   **复用相同患者号时视为不同患者**，各自派生独立的伪名与日期偏移；而**同一签发机构 +
   同一患者号**在文件之间、批次之间仍稳定归为同一患者，跨批次得到相同结果。
2. **非空 `PatientID`、签发机构缺失或为空白**：身份键保持为 `id:<患者号>`，
   与升级前完全一致——既有批次的伪名、日期偏移结果不变。
3. **没有 `PatientID` 时逐级回退**：`name:<大写 PatientName>` →
   `study:<StudyInstanceUID>` → `file:<文件内容哈希>`；后两者会给出
   `PATIENT_IDENTITY_FALLBACK` 警告。

原 `IssuerOfPatientID` **不会留在匿名 DICOM 中**（整标签 `REMOVE`；
`IssuerOfPatientIDQualifiersSequence`、`OtherPatientIDs` /
`OtherPatientIDsSequence` 等关联身份字段同样删除）。

跨任务一致性核对按同一身份键对齐：`id:HOSP-A␟P-9` 与 `id:HOSP-B␟P-9`
是**两个不同的键**——即使患者号相同，也不会被误判为同一患者；它们在批次间各自只出现
一次时仅作为覆盖范围差异（`only_in_one_job`）列出，不产生伪名冲突。仅当
签发机构与患者号都相同的键在不同任务派生出不同伪名时，才报告 `conflict`。

### ZIP 同名条目与重复实例

ZIP 中央目录允许出现**同名条目**（例如两个 `scan.dcm`，或 `./scan.dcm` 与
`scan.dcm` 这类规范化后同址的名称）。服务按 **ZIP 条目**（而非成员名）解压：
每个条目写入以条目序号命名的独立暂存文件，后续两遍 UID 收集、患者分组与匿名
写出都读取该条目自己的字节——**同名条目不会互相覆盖，也不会重复处理末项**。

逐文件结果（`files[]`）中：

- `name` 为 **ZIP 原始成员名**（未规范化，同名条目会看到相同的名字）；
- `entry_index` 为该条目在 ZIP 中央目录中的 **0 基序号**，同名条目以此区分。

写出规则：

- **同名不同 SOPInstanceUID**：各自正常写出为 `<新SOP UID>.dcm`，互不干扰；
- **同名同 SOPInstanceUID**：属于重复实例，沿用现有规则——首个写出为
  `<新SOP UID>.dcm`，后续依次改名 `<新SOP UID>-2.dcm`、`-3.dcm`……并给出
  `DUPLICATE_SOP_INSTANCE` 警告（见[诊断码](#诊断码)）。

`../` 路径穿越、绝对路径与符号链接/设备等非常规条目仍然整体拒绝；解压总大小
与条目数量上限不变（见[配置](#配置环境变量)）。

### 按匿名 StudyInstanceUID 下载检查子集

除全量下载外，还可以只下载某一个匿名检查，并由服务端**沿 SOP 引用自动补全**
该检查依赖的实例。子集下载**直接复用已完成任务 output 目录中的匿名 DICOM 文件，
不会重新解压或处理原始 ZIP**。

`study_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}`
响应里的 `studies[].new_study_instance_uid`（或某文件的 `study_instance_uid`）。

闭包补全规则：

1. 以该匿名检查的**全部成功输出实例**作为初选集合；
2. 递归读取集合内匿名 DICOM **任意序列层级**的 `ReferencedSOPInstanceUID`
   （`ReferencedSOPSequence`、RT 的 `ContourImageSequence`、
   `ReferencedFrameOfReferenceSequence` 等），把**同任务内**被引用的实例补入集合，
   引用目标可以属于**其他匿名检查**（跨检查引用同样补入），重复扫描直至闭包稳定；
3. 只要存在引用目标**不在成功输出中**、**一个匿名 SOP UID 对应多个输出文件**
   （输入 ZIP 内重复实例），或**任务成功输出记录对应的物理文件已从 output_dir
   丢失**，接口返回 `409`，在 `detail.problems[]` 中给出**匿名 SOP UID、原因与
   缺失/冲突的 output_files**，**不输出残缺 ZIP**；
4. 匿名检查在任务成功输出中不存在时返回 `404`（任务不存在同为 `404`）。

成功时返回扁平 ZIP（文件名即匿名 `SOPInstanceUID`，与全量包内文件**逐字节相同**），
并通过响应头给出初选与补入文件数：

| 响应头 | 含义 |
| --- | --- |
| `X-Study-Initial-File-Count` | 初选文件数（该匿名检查的全部实例） |
| `X-Study-Added-File-Count` | 递归补入的文件数（含跨检查引用） |
| `X-Study-Subset-File-Count` | ZIP 总文件数（初选 + 补入） |

```bash
# 4) 取某个匿名 StudyInstanceUID（new_study_instance_uid），下载检查子集
STUDY_UID=$(python3 -c "import json;print(json.load(open('job.json'))['studies'][0]['new_study_instance_uid'])")

curl -OJ -D subset.headers \
  "http://localhost:8000/jobs/$JOB_ID/studies/$STUDY_UID/download"
grep -i '^x-study-' subset.headers
# x-study-initial-file-count: 3
# x-study-added-file-count: 1
# x-study-subset-file-count: 4
```

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/studies/{study_uid}/download"
)
if resp.status_code == 404:
    print("任务或匿名检查不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("引用闭包无法闭合：", detail["code"])
    for p in detail["problems"]:
        # reason: not_in_output（引用目标不在成功输出中）
        #       | ambiguous_output（同一 SOP UID 对应多个输出文件）
        #       | output_missing（成功输出记录的物理文件已丢失）
        print(p["sop_instance_uid"], p["reason"], p.get("output_files"))
else:
    resp.raise_for_status()
    print("初选：", resp.headers["X-Study-Initial-File-Count"],
          "补入：", resp.headers["X-Study-Added-File-Count"])
    open("study-subset.zip", "wb").write(resp.content)
```

> 注意：匿名化时的悬空引用（`DANGLING_REFERENCE` 警告）会使引用闭包无法在本任务
> 输出内闭合，因此含此类引用的检查子集下载会返回 `409`；请改全量下载
> `GET /jobs/{id}/download`（行为不变），或在上传前补齐被引用实例。

### 下载带 DICOMDIR 的离线阅片 ZIP（`GET /jobs/{job_id}/dicomdir/download`）

需要把匿名结果导入仅支持**介质存储浏览**（DICOMDIR）的离线阅片软件时，可用本接口
取回一个根目录含 `DICOMDIR` 的 ZIP。构建**只读取已完成任务 output 目录中的匿名
DICOM 文件**，不重新解压或处理原始 ZIP：

1. 逐个读取匿名输出，按文件**自身标签**重建
   `患者（PatientID/PatientName 伪名）→ 检查（StudyInstanceUID）→
   系列（SeriesInstanceUID）→ 实例（SOPInstanceUID）` 四级层级；
2. 每个实例被映射为符合 DICOM File ID 约束（每组件 ≤ 8 字符、大写字母数字、
   最多 8 级）的**稳定短路径**，形如 `PT000000/ST000000/SExxxxxx/IMxxxxxx`
   （RT 结构集等为 `RSxxxxxx`）；同一任务的同一批输出按
   患者伪名 ID → Study/Series/SOP UID 的固定键排序编号，**重复请求得到相同路径**；
3. DICOMDIR 叶级记录的 `ReferencedFileID` 必须指向 ZIP 内**实际存在**的文件，
   写出后会重新加载逐条校验，实例文件与全量匿名包内文件**逐字节相同**；
4. ZIP 内**只含匿名伪名与匿名 UID**：不带入任何原始目录结构、原始文件名、
   患者真实身份字段。

```bash
# 5) 下载离线阅片包（解压后根目录即 DICOMDIR，可直接被阅片软件作为介质打开）
curl -OJ -D dicomdir.headers \
  "http://localhost:8000/jobs/$JOB_ID/dicomdir/download"
grep -i '^x-dicomdir-' dicomdir.headers
# x-dicomdir-file-count: 4
# x-dicomdir-patient-count: 2
# x-dicomdir-study-count: 2
# x-dicomdir-series-count: 3

unzip -l dicomdir-*.zip
# DICOMDIR
# PT000000/ST000000/SE000000/IM000000
# PT000000/ST000000/SE000000/IM000001
# PT000001/ST000000/SE000001/RS000000
```

建目录必需的标签：患者伪名 `PatientID` / `PatientName`、
`StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID`、`SOPClassUID`、
文件元信息 `TransferSyntaxUID`、以及系列级 `Modality`。其余描述字段
（`StudyDate` / `SeriesNumber` / `InstanceNumber` 等）存在且非空时带入目录记录，
缺失不影响建目录（目录不会为其合成值）。

出现以下任一情况时接口**不产生残缺 ZIP**，而是返回 `409`
（`detail.code = DICOMDIR_BUILD_INCOMPLETE`），在 `detail.problems[]` 中
**一次性列出全部问题文件及原因**：

| `reason` | 触发条件 |
| --- | --- |
| `unreadable` | 匿名输出文件存在但无法作为 DICOM 读取（损坏 / 截断 / 编码错误） |
| `missing_required_tags` | 缺少建立目录必需的标签（`missing_tags` 给出标签关键字列表） |
| `ambiguous_output` | 同一匿名 SOPInstanceUID 对应多份输出文件（`output_files` 给出冲突文件名），无法分配唯一 File ID |
| `output_missing` | 任务记录中有该输出，但磁盘文件已不存在 |

任务不存在 / 已过期返回 `404`；任务未完成（`status != ready`）返回 `409`
（`JOB_NOT_READY`）；任务没有任何成功匿名输出时返回 `409`
（`NO_ANONYMIZED_OUTPUT`）。全量下载 `GET /jobs/{id}/download` 不受这些限制影响。

Python 示例（含 409 处理）：

```python
resp = requests.get(f"http://localhost:8000/jobs/{job_id}/dicomdir/download")
if resp.status_code == 409:
    detail = resp.json()["detail"]
    print("拒绝输出目录包：", detail["code"])
    for p in detail.get("problems", []):
        # p["reason"]: unreadable | missing_required_tags
        #            | ambiguous_output | output_missing
        print(p["output_file"], p["reason"], p.get("missing_tags"),
              p.get("output_files"))
else:
    resp.raise_for_status()
    print("患者：", resp.headers["X-Dicomdir-Patient-Count"],
          "实例：", resp.headers["X-Dicomdir-File-Count"])
    open("dicomdir.zip", "wb").write(resp.content)
```

### 实例影像 PNG 预览（`GET /jobs/{job_id}/instances/{sop_instance_uid}/preview.png`）

匿名化完成后，如需在导入 PACS 前**人工抽查影像内容本身**（而不只是标签），可按
**匿名 SOPInstanceUID** 取回该实例的 8 位灰度 PNG。数据**只来自已完成任务 output
目录中的匿名 DICOM 文件**，像素即匿名结果中的实际像素（匿名化不重编码、不改像素），
不重新解压或处理原始 ZIP。

`sop_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}` 响应里
`files[].sop_instance_uid`（成功文件的 `output_name` 去掉 `.dcm` 也是它）。

**渲染管线**（严格按以下顺序）：

1. 应用 Modality LUT 的线性形式：`value = pixel × RescaleSlope + RescaleIntercept`
   （缺省 slope=1、intercept=0）；
2. 按 `WindowCenter` / `WindowWidth` 的**首项**（VM>1 时只取第 1 个值）依
   DICOM PS3.3 C.11.2.1.2 标准线性公式映射到 8 位灰度；两个标签任一缺失/为空时
   **改按像素最小/最大值线性拉伸**（常量影像为全黑）；
3. `MONOCHROME1`（值越大越暗）在**最后**对灰度反相；`MONOCHROME2` 不反相。

**支持范围**（范围外影像明确报错，不静默返回错误内容、也不静默挑文件）：

| 条件 | 要求 |
| --- | --- |
| 传输语法 | 仅未压缩 native：Implicit VR Little Endian、Explicit VR Little Endian、Explicit VR Big Endian；JPEG / JPEG-LS / JPEG 2000 / RLE 等封装语法与 Deflated Explicit VR 一律拒绝 |
| 帧数 | 单帧（`NumberOfFrames` 缺省或为 1；>1 拒绝） |
| 采样 | `SamplesPerPixel=1` |
| 光度解释 | `MONOCHROME1` / `MONOCHROME2`（RGB / PALETTE COLOR / YBR 等拒绝） |
| 位深 | `BitsAllocated=8/16`，且 `BitsStored/HighBit/PixelRepresentation` 合法（pydicom 负责按 BitsStored 掩码与符号扩展） |

像素数据缺失（如 RTSTRUCT 等非影像对象）、PixelData 截断或与
Rows/Columns/BitsAllocated 不符而无法解码、Rescale/Window 取值非法时同样返回
`422`。

同一匿名 SOP UID 对应**多份输出文件**（输入 ZIP 内重复实例，引擎已改名为
`<uid>.dcm` / `<uid>-2.dcm`）时**不挑选、不猜测**，返回 `409` 并在
`output_files` 中列出冲突文件；该 UID 不在任务成功输出中返回 `404`。

错误响应（HTTP 4xx，`detail.code` 机器可读，`detail.reason` 给出 422 的细分原因）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任务 ID 不存在或已过期 |
| `404` | `INSTANCE_NOT_FOUND` | 该匿名 SOPInstanceUID 不在任务的成功输出中 |
| `404` | `INSTANCE_OUTPUT_MISSING` | 任务记录有该输出但磁盘文件已不存在（`reason=output_missing`） |
| `409` | `JOB_NOT_READY` | 任务 `status != ready`（匿名化处理失败） |
| `409` | `AMBIGUOUS_INSTANCE_OUTPUT` | 同一匿名 SOP UID 对应多份输出文件（`reason=ambiguous_output`，`output_files` 列出冲突文件） |
| `422` | `PREVIEW_UNSUPPORTED_IMAGE` | 无法解码或影像类型不支持；`reason` 取值：`unreadable`、`unsupported_transfer_syntax`、`unsupported_samples_per_pixel`、`unsupported_photometric_interpretation`、`multiframe`、`unsupported_bit_depth`、`invalid_pixel_metadata`、`missing_pixel_data`、`pixel_decode_failed`、`invalid_rescale`、`invalid_voi_window`、`sop_instance_uid_mismatch` |

成功时返回 `image/png`（`Content-Disposition: inline`，可直接在浏览器打开核对），
并通过响应头回传渲染参数：

| 响应头 | 含义 |
| --- | --- |
| `X-Preview-Rows` / `X-Preview-Columns` | 图像行/列数 |
| `X-Preview-Bits-Allocated` / `X-Preview-Bits-Stored` | 源影像分配/有效位深（8/16） |
| `X-Preview-Photometric-Interpretation` | `MONOCHROME1` / `MONOCHROME2` |
| `X-Preview-Window` | `voi`（使用 WindowCenter/Width 首项）或 `minmax`（缺窗按像素极值拉伸） |
| `X-Preview-Rescale-Slope` / `X-Preview-Rescale-Intercept` | 实际使用的 Modality 线性映射参数 |

```bash
# 6) 取某个成功输出文件的匿名 SOPInstanceUID，在浏览器或 curl 中查看 PNG
SOP_UID=$(python3 -c "
import json
job=json.load(open('job.json'))
print(next(f['sop_instance_uid'] for f in job['files'] if f['status']=='ok'))")

curl -f "http://localhost:8000/jobs/$JOB_ID/instances/$SOP_UID/preview.png" -o preview.png
xdg-open preview.png   # 或直接在浏览器访问该 URL

# 或直接用输出文件名（去掉 .dcm 即匿名 SOP UID）
curl -f "http://localhost:8000/jobs/$JOB_ID/instances/$SOP_UID/preview.png" \
  -D - -o preview.png | grep -i '^x-preview-'
# x-preview-rows: 4
# x-preview-columns: 4
# x-preview-bits-allocated: 16
# x-preview-bits-stored: 16
# x-preview-photometric-interpretation: MONOCHROME2
# x-preview-window: minmax
# x-preview-rescale-slope: 1.0
# x-preview-rescale-intercept: 0.0
```

Python 示例（含 404/409/422 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/instances/{anon_sop_uid}/preview.png"
)
if resp.status_code == 404:
    print("任务或匿名实例不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("无法预览：", detail["code"])          # JOB_NOT_READY / AMBIGUOUS_INSTANCE_OUTPUT
    print("冲突文件：", detail.get("output_files"))
elif resp.status_code == 422:
    detail = resp.json()["detail"]
    print("影像无法解码或不受支持：", detail["code"], detail["reason"], detail["message"])
    # reason: unreadable | unsupported_transfer_syntax | unsupported_samples_per_pixel |
    #         unsupported_photometric_interpretation | multiframe | unsupported_bit_depth |
    #         invalid_pixel_metadata | missing_pixel_data | pixel_decode_failed |
    #         invalid_rescale | invalid_voi_window | sop_instance_uid_mismatch
else:
    resp.raise_for_status()
    open("preview.png", "wb").write(resp.content)
    print("窗模式：", resp.headers["X-Preview-Window"])
```

> 注意：预览只做线性 Modality LUT + 线性 VOI 窗，**不应用** Presentation LUT、
> 不应用 LUTData、不做几何变换（不缩放、不旋转、不叠加任何标注），目的仅为快速
> 人工核对影像内容；需要诊断级阅片请下载匿名 ZIP / DICOMDIR 包用专业阅片软件打开。

### 系列切片几何分析（`GET /jobs/{job_id}/series/{series_instance_uid}/geometry`）

匿名化后导入 PACS / 重建前，如需检查一个断层系列的**层方向是否一致、切片是否
按几何位置有序排列、是否存在异常层间距（漏层/重层）**，可按**匿名
SeriesInstanceUID** 取回该系列的几何分析结果。数据**只来自已完成任务 output
目录中的匿名 DICOM 文件**，不重新解压或处理原始 ZIP，且**只纳入成功写出的
单帧实例**（`NumberOfFrames` 缺省或为 1；多帧实例不参与分析，在
`excluded_instances` 中单独列出）。

`series_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}`
响应里的 `series[].new_series_instance_uid`（或某文件的 `series_instance_uid`）。

**分析算法**：

1. 读取每个单帧实例的 `ImageOrientationPatient`（行、列方向余弦，各 3 分量）与
   `ImagePositionPatient`（片层坐标，毫米）；
2. 校验方向余弦为有限值、分量在 `[-1, 1]` 内、行/列向量非零且近似正交，再做
   单位化并求叉积法线 ``n = row × col``；
3. 把各实例位置投影到法线：``p = ImagePositionPatient · n``，按 `p` **升序**
   排列切片；
4. 计算排序后逐对相邻投影距离，以其**中位数**为基准间距，把
   **`distance > 1.5 × median`** 的相邻间隙标记为异常缺口（严格大于；恰好等于
   1.5 倍不算）；
5. 有效单帧实例**不足 3 张**（相邻间距不足 2 个）时 `gap_check` 返回
   `insufficient_slices`，`median_spacing` / `anomalous_gap_indices` 为空，
   并在 `message` 中注明无法判断间隙；其余情况 `gap_check=ok`。

成功响应（200）：

| 字段 | 说明 |
| --- | --- |
| `frame_of_reference_uid` | 系列内统一的匿名 FrameOfReferenceUID |
| `image_orientation_patient` | 系列基准方向余弦（6 分量，已单位化） |
| `normal` | 行 × 列叉积得到的单位法线（3 分量） |
| `instance_count` | 参与排序的有效单帧实例数 |
| `gap_check` | `ok` / `insufficient_slices` |
| `median_spacing` | 相邻投影距离中位数（毫米）；不足 3 张时为 `null` |
| `gap_factor` | 缺口阈值倍数，固定 `1.5` |
| `anomalous_gap_indices` | 异常间隙序号列表（对应 `spacings[].index`） |
| `slices[]` | 排序后的 `index` / 匿名 `sop_instance_uid` / `projection` / `image_position_patient` |
| `spacings[]` | 逐对相邻切片：`index`、`from/to_sop_instance_uid`、`distance`（毫米）、`anomalous` |
| `excluded_instances[]` | 未参与分析的实例（`reason=multiframe`，附 `number_of_frames`） |
| `message` | `insufficient_slices` 时的说明，正常时为 `null` |

```bash
# 7) 取某个匿名 SeriesInstanceUID，查看投影排序与异常层间距
SERIES_UID=$(python3 -c "import json;print(json.load(open('job.json'))['series'][0]['new_series_instance_uid'])")

curl -f "http://localhost:8000/jobs/$JOB_ID/series/$SERIES_UID/geometry" | python3 -m json.tool
```

```jsonc
{
  "series_instance_uid": "2.25.1695…",
  "frame_of_reference_uid": "2.25.4467…",
  "image_orientation_patient": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
  "normal": [0.0, 0.0, 1.0],                 // 轴位：法线 +Z
  "instance_count": 4,
  "gap_check": "ok",
  "median_spacing": 5.0,
  "gap_factor": 1.5,
  "anomalous_gap_indices": [2],              // 第 3 个间隙 20mm > 7.5mm
  "slices": [
    {"index": 0, "sop_instance_uid": "2.25.1528…", "projection": 0.0,
     "image_position_patient": [10.0, -20.0, 0.0]},
    {"index": 1, "sop_instance_uid": "2.25.3174…", "projection": 5.0,
     "image_position_patient": [10.0, -20.0, 5.0]},
    {"index": 2, "sop_instance_uid": "2.25.3258…", "projection": 10.0,
     "image_position_patient": [10.0, -20.0, 10.0]},
    {"index": 3, "sop_instance_uid": "2.25.2030…", "projection": 30.0,
     "image_position_patient": [10.0, -20.0, 30.0]}
  ],
  "spacings": [
    {"index": 0, "from_sop_instance_uid": "2.25.1528…", "to_sop_instance_uid": "2.25.3174…",
     "distance": 5.0, "anomalous": false},
    {"index": 1, "from_sop_instance_uid": "2.25.3174…", "to_sop_instance_uid": "2.25.3258…",
     "distance": 5.0, "anomalous": false},
    {"index": 2, "from_sop_instance_uid": "2.25.3258…", "to_sop_instance_uid": "2.25.2030…",
     "distance": 20.0, "anomalous": true}
  ],
  "message": null,
  "excluded_instances": []
}
```

出现以下任一情况时接口**不返回不可靠的排序结果**，而是返回 `409`
（`detail.code = SERIES_GEOMETRY_CONFLICT`），在 `detail.problems[]` 中
**一次性列出全部冲突，且每条都带匿名 SOP UID**（绝不回显原始 UID）：

| `reason` | 触发条件 |
| --- | --- |
| `frame_of_reference_mismatch` | 系列内 `FrameOfReferenceUID` 不一致（分组给出各组匿名 SOP UID、本组与基准 Frame UID） |
| `orientation_mismatch` | `ImageOrientationPatient` 方向余弦不一致（给本组与基准的 6 分量、叉积法线；法线反向时 `opposite_direction=true`） |
| `duplicate_projection` | 两个及以上实例的法线投影重合（`<= 1e-6 mm`，无法唯一排序，给出各匿名 SOP UID 及投影坐标） |
| `invalid_geometry` | 几何标签缺失/无法解析/取值非法（缺标签时在 `missing_or_invalid_tags` 给出 `FrameOfReferenceUID` / `ImageOrientationPatient` / `ImagePositionPatient`；如零向量、非正交、分量越界、分量数不对） |
| `ambiguous_output` | 同一匿名 SOP UID 对应多份物理输出（`output_files` 列出冲突文件） |
| `unreadable` / `output_missing` | 匿名输出不可读，或任务记录有输出但磁盘文件已不存在 |

> 说明：非影像对象（如 RTSTRUCT）没有几何标签时按 `invalid_geometry` 报出；
> 多帧实例（`NumberOfFrames` 显式 `>1` 或取值非法）不属于“单帧实例”，
> 不算冲突，只在成功响应的 `excluded_instances` 中列出。

任务不存在 / 已过期返回 `404`（`JOB_NOT_FOUND`）；任务未完成返回 `409`
（`JOB_NOT_READY`）；该匿名系列在任务的成功输出中不存在（含系列文件全部
失败、无任何成功输出）返回 `404`（`SERIES_NOT_FOUND`）。系列有成功输出但
**全部是多帧实例**、没有任何可分析的单帧实例时返回 `409`
（`NO_SINGLE_FRAME_INSTANCES`，`problems[]` 列出被排除的多帧实例）。

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/series/{anon_series_uid}/geometry"
)
if resp.status_code == 404:
    print("任务或匿名系列不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    if detail["code"] == "JOB_NOT_READY":
        print("任务未完成：", detail["message"])
    else:
        print("几何冲突：", detail["code"])
        for p in detail["problems"]:
            # reason: frame_of_reference_mismatch | orientation_mismatch |
            #         duplicate_projection | invalid_geometry |
            #         ambiguous_output | unreadable | output_missing
            print(p["reason"], p.get("sop_instance_uids") or p.get("sop_instance_uid"))
else:
    resp.raise_for_status()
    g = resp.json()
    if g["gap_check"] == "insufficient_slices":
        print(g["message"])
    else:
        print("中位层间距：", g["median_spacing"], "mm")
        print("异常间隙：",
              [g["spacings"][i] for i in g["anomalous_gap_indices"]])
```

### 系列 CT 管电压（KVP）逐帧分布（`GET /jobs/{job_id}/series/{series_instance_uid}/kvp`）

匿名化后导入 PACS 前，如需核对一个 CT 系列的**管电压是否一致**（例如发现同一
系列混用了 80kV 与 120kV 扫描参数），可按**匿名 SeriesInstanceUID** 取回该系列
的 KVP 逐帧分布。数据**只来自已完成任务 output 目录中的匿名 DICOM 文件**，
不重新解压或处理原始 ZIP，单帧与多帧（增强 CT）实例都纳入统计。

`series_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}`
响应里的 `series[].new_series_instance_uid`（或某文件的 `series_instance_uid`）。

**逐帧解析规则**（每个实例逐帧取 KVP，三级回退）：

1. 优先取该帧对应 `PerFrameFunctionalGroupsSequence[i]` 内
   `CTExposureSequence[0].KVP`（增强 CT 逐帧功能组）；
2. 其次取 `SharedFunctionalGroupsSequence[0].CTExposureSequence[0].KVP`
   （共享功能组）；
3. 最后取顶层 `KVP`（0018,0060，经典 CT）。

`NumberOfFrames` 缺省或为 1 的实例按**一帧**处理。某一层级的 KVP 标签存在且
非空即采用该层级的值；取值无法解析为有限数值（如 VM>1 的畸形值）时该帧计入
缺失，**不再向下回退**（避免用其他层级的值掩盖损坏数据）；三层都没有 KVP 的
帧同样计入缺失。

成功响应（200）：

| 字段 | 说明 |
| --- | --- |
| `instance_count` / `total_frames` | 纳入统计的实例数 / 总帧数 |
| `missing_kvp_frames` | 全系列缺失（或无法解析）KVP 的帧数 |
| `mixed_kvp` | 系列内是否混用多种管电压（distinct KVP > 1） |
| `voltage_distribution[]` | 各电压的帧数：`kvp`（kV）+ `frame_count`，按 kV 升序 |
| `instances[]` | 逐实例明细，**按匿名 SOPInstanceUID 字典序排列**：`number_of_frames`、`kvp_source`（`per_frame` / `shared` / `top_level` / `mixed` / `none`）、逐帧 `frame_kvps`（`null` 表示该帧缺失）、`missing_kvp_frames` |

```bash
# 8) 取某个匿名 SeriesInstanceUID，查看 KVP 分布（默认端口 8000）
SERIES_UID=$(python3 -c "import json;print(json.load(open('job.json'))['series'][0]['new_series_instance_uid'])")

curl -f "http://localhost:8000/jobs/$JOB_ID/series/$SERIES_UID/kvp" | python3 -m json.tool
```

```jsonc
{
  "series_instance_uid": "2.25.1695…",
  "instance_count": 2,
  "total_frames": 5,
  "missing_kvp_frames": 0,
  "mixed_kvp": true,                        // 同系列混用了 80kV 与 120kV
  "voltage_distribution": [
    {"kvp": 80.0, "frame_count": 1},
    {"kvp": 120.0, "frame_count": 4}
  ],
  "instances": [
    {"sop_instance_uid": "2.25.1528…", "output_file": "2.25.1528….dcm",
     "number_of_frames": 1, "kvp_source": "top_level",
     "frame_kvps": [120.0], "missing_kvp_frames": 0},
    {"sop_instance_uid": "2.25.3174…", "output_file": "2.25.3174….dcm",
     "number_of_frames": 4, "kvp_source": "per_frame",
     "frame_kvps": [120.0, 120.0, 80.0, 120.0], "missing_kvp_frames": 0}
  ]
}
```

出现以下任一情况时接口**不返回不完整统计**，而是返回 `409`
（`detail.code = SERIES_KVP_CONFLICT`），在 `detail.problems[]` 中
**一次性列出全部冲突，且每条都带匿名 SOP UID**（绝不回显原始 UID）：

| `reason` | 触发条件 |
| --- | --- |
| `frame_count_mismatch` | `NumberOfFrames` 声明帧数与 `PerFrameFunctionalGroupsSequence` 条目数不一致（`declared_number_of_frames` / `per_frame_item_count` 给出两侧数值） |
| `invalid_number_of_frames` | `NumberOfFrames` 取值非法（非正整数），无法确定帧数 |
| `ambiguous_output` | 同一匿名 SOP UID 对应多份物理输出（`output_files` 列出冲突文件） |
| `unreadable` / `output_missing` | 匿名输出不可读，或任务记录有输出但磁盘文件已不存在 |

任务不存在 / 已过期返回 `404`（`JOB_NOT_FOUND`）；任务未完成返回 `409`
（`JOB_NOT_READY`）；该匿名系列在任务的成功输出中不存在返回 `404`
（`SERIES_NOT_FOUND`）。

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/series/{anon_series_uid}/kvp"
)
if resp.status_code == 404:
    print("任务或匿名系列不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    if detail["code"] == "JOB_NOT_READY":
        print("任务未完成：", detail["message"])
    else:
        print("KVP 统计冲突：", detail["code"])
        for p in detail["problems"]:
            # reason: frame_count_mismatch | invalid_number_of_frames |
            #         ambiguous_output | unreadable | output_missing
            print(p["reason"], p.get("sop_instance_uid"))
else:
    resp.raise_for_status()
    k = resp.json()
    if k["mixed_kvp"]:
        print("警告：同系列混用多种管电压！")
    for bucket in k["voltage_distribution"]:
        print(f"{bucket['kvp']} kV: {bucket['frame_count']} 帧")
    print("缺失 KVP 帧数：", k["missing_kvp_frames"])
```

### CT 剂量 SR 照射事件解析（`GET /jobs/{job_id}/instances/{sop_instance_uid}/dose-events`）

匿名化完成后，如需核对某份 **X-Ray Radiation Dose SR**（CT 剂量报告）中的逐次照射剂量，
可按**匿名 SOPInstanceUID** 解析该 SR 的 CT 照射事件。数据**只来自已完成任务 output
目录中的匿名 DICOM 文件**，不重新解压或处理原始 ZIP。

`sop_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}` 响应里
`files[].sop_instance_uid`（成功文件的 `output_name` 去掉 `.dcm` 也是它）。

**解析规则**：

1. 文件必须是 X-Ray Radiation Dose SR（`SOPClassUID = 1.2.840.10008.5.1.4.1.1.88.67`）
   且含 `ContentSequence`；
2. 递归遍历 `ContentSequence`：`ConceptNameCodeSequence` 为
   **(113812, DCM, "CT Irradiation Event Data")** 的容器划定一个 CT 照射事件边界；
3. 事件边界内（任意嵌套深度）的 **(113829, DCM, "CTDIvol")** 与
   **(113838, DCM, "DLP")** NUM 项归属**最近（内层）事件**；事件边界之外的同类数值
   （报告级累计值，如 CT Accumulated Dose Data 容器内的 CT Dose Length Product Total）
   **一律忽略**，不进入事件也不参与合计；
4. 每个事件必须恰好含一个 CTDIvol（单位 `mGy`）与一个 DLP（单位 `mGy.cm`，UCUM 编码）。

成功响应（200）：

| 字段 | 说明 |
| --- | --- |
| `sop_instance_uid` / `output_file` | 请求的匿名 SOPInstanceUID / 被解析的扁平输出文件名 |
| `event_count` | 解析出的 CT 照射事件数（SR 中无 CT 事件时合法返回 0） |
| `total_dlp_mgy_cm` | 全部**事件** DLP 之和（mGy·cm）；报告级累计值不参与合计 |
| `events[]` | 按 SR **内容顺序**排列的事件：`index`（0 基）、`ctdi_vol_mgy`、`dlp_mgy_cm` |

```bash
# 9) 取某个剂量 SR 的匿名 SOPInstanceUID，解析 CT 照射事件剂量（默认端口 8000）
SR_UID=$(python3 -c "
import json
job=json.load(open('job.json'))
print(next(f['sop_instance_uid'] for f in job['files'] if f['status']=='ok'))")

curl -f "http://localhost:8000/jobs/$JOB_ID/instances/$SR_UID/dose-events" | python3 -m json.tool
```

```jsonc
{
  "sop_instance_uid": "2.25.1528…",
  "output_file": "2.25.1528….dcm",
  "event_count": 2,
  "total_dlp_mgy_cm": 707.0,                 // 仅事件 DLP 之和，不含报告级累计值
  "events": [
    {"index": 0, "ctdi_vol_mgy": 12.5, "dlp_mgy_cm": 456.5},
    {"index": 1, "ctdi_vol_mgy": 8.25, "dlp_mgy_cm": 250.5}
  ]
}
```

事件内**缺项、重复项、非有限数值或单位不符**时，接口**不输出部分事件或部分合计**，
而是返回 `409`（`detail.code = DOSE_SR_CONFLICT`），在 `detail.problems[]` 中
**一次性列出全部问题**，每条都带匿名 SOP UID、`event_index`、`quantity`
（`ctdi_vol` / `dlp`）与机器可读 `reason`：

| `reason` | 触发条件 |
| --- | --- |
| `missing_value` | 事件内缺少 CTDIvol / DLP 数值项 |
| `duplicate_value` | 事件内同一量出现多次，无法确定唯一取值 |
| `invalid_value` | 数值缺失或无法解析为有限数值（`numeric_value` 给出原始文本） |
| `unit_mismatch` | 单位不符：CTDIvol 须为 `mGy`、DLP 须为 `mGy.cm`（`units` 给出实际单位） |
| `unreadable` | 匿名输出不可读（损坏 / 截断 / 编码错误） |
| `not_dose_sr` | 实例不是 X-Ray Radiation Dose SR，或缺少 `ContentSequence` |
| `sop_instance_uid_mismatch` | 文件内 SOPInstanceUID 与请求的匿名 UID 不一致 |

其余错误（`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任务 ID 不存在或已过期 |
| `404` | `INSTANCE_NOT_FOUND` | 该匿名 SOPInstanceUID 不在任务的成功输出中 |
| `404` | `INSTANCE_OUTPUT_MISSING` | 任务记录有该输出但磁盘文件已不存在（`reason=output_missing`） |
| `409` | `JOB_NOT_READY` | 任务 `status != ready`（匿名化处理失败） |
| `409` | `AMBIGUOUS_INSTANCE_OUTPUT` | 同一匿名 SOP UID 对应多份输出文件（`output_files` 列出冲突文件） |

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/instances/{anon_sop_uid}/dose-events"
)
if resp.status_code == 404:
    print("任务或匿名实例不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("无法解析剂量事件：", detail["code"])
    for p in detail.get("problems", []):
        # reason: missing_value | duplicate_value | invalid_value |
        #         unit_mismatch | unreadable | not_dose_sr | sop_instance_uid_mismatch
        print(p["reason"], "event:", p.get("event_index"), p.get("quantity"))
else:
    resp.raise_for_status()
    d = resp.json()
    for e in d["events"]:
        print(f"事件 {e['index']}: CTDIvol {e['ctdi_vol_mgy']} mGy, "
              f"DLP {e['dlp_mgy_cm']} mGy·cm")
    print("事件 DLP 合计：", d["total_dlp_mgy_cm"], "mGy·cm")
```

### SEG 分割覆盖率统计（`GET /jobs/{job_id}/instances/{sop_instance_uid}/seg-coverage`）

匿名化完成后，如需核对某份 **Segmentation（SEG）** 各段在源图像上的覆盖范围，
可按**匿名 SOPInstanceUID** 统计其分割覆盖率。数据**只来自已完成任务 output
目录中的匿名 DICOM 文件**，不重新解压或处理原始 ZIP。

`sop_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}` 响应里
`files[].sop_instance_uid`（成功文件的 `output_name` 去掉 `.dcm` 也是它）。

**支持范围**（范围外一律 `409`，不静默返回错误内容）：

| 条件 | 要求 |
| --- | --- |
| SOP Class | Segmentation Storage（`1.2.840.10008.5.1.4.1.1.66.4`） |
| 分割类型 | `SegmentationType=BINARY`（FRACTIONAL 拒绝） |
| 传输语法 | 仅未压缩 native：Implicit/Explicit VR Little Endian、Explicit VR Big Endian；JPEG / RLE / JPEG 2000 等封装语法拒绝 |
| 位深 | `BitsAllocated=BitsStored=1`、`HighBit=0`（1-bit 掩码） |

**统计规则**：

1. 逐帧读取 `PerFrameFunctionalGroupsSequence[i]`：
   `SegmentIdentificationSequence[0].ReferencedSegmentNumber` 给出**段号**；
   遍历该帧**全部** `DerivationImageSequence` 项及其**全部**
   `SourceImageSequence` 项，收集 `ReferencedSOPInstanceUID`
   （匿名化已把序列内引用与源实例一致重写，因此可直接对齐任务成功输出）并按
   匿名 SOP UID **去重**：
   - 恰有一个唯一 UID 时，该帧归属对应源图像，沿用唯一源图像校验与掩码并集；
   - **同一帧引用两个及以上不同匿名 SOP UID**（例如多源分割：不同
     DerivationImageSequence 项，或同一 SourceImageSequence 内多个源项）时，
     无法唯一确定该帧归属，接口返回 `409`（`multiple_source_references`，
     逐帧给出冲突 UID 列表），**不会静默把该帧归给第一个引用**；
   - 完全没有源引用（缺 DerivationImageSequence / SourceImageSequence /
     ReferencedSOPInstanceUID）时返回 `409`（`missing_source_reference`）；
2. 每个被引用的源图像必须**唯一**对应任务内一份成功输出，且其
   `Rows/Columns` 与 SEG 掩码一致（只读取源图像像素之前的标签，不解码源像素）；
3. 解码 1-bit 掩码，把**同一（源图像, 段号）** 的多帧**逐像素取并集**
   （重叠区域只计一次，不是简单相加）；
4. 按（源 SOP UID, 段号）排序返回每组的前景像素数、图像像素数
   （源图像 `Rows × Columns`）与覆盖率。

成功响应（200）：

| 字段 | 说明 |
| --- | --- |
| `sop_instance_uid` / `output_file` | 请求的匿名 SEG SOPInstanceUID / 被统计的扁平输出文件名 |
| `segmentation_type` | 固定 `BINARY` |
| `rows` / `columns` / `number_of_frames` | SEG 掩码行/列数与声明帧数 |
| `group_count` | （源图像, 段号）分组数 |
| `groups[]` | 按（源 SOP UID, 段号）排序：`source_sop_instance_uid`、`source_output_file`、`segment_number`、`frame_count`（参与并集的帧数）、`foreground_pixels`（并集前景像素数）、`image_pixels`、`coverage`（= foreground/image_pixels，0..1） |

```bash
# 10) 取某个 SEG 的匿名 SOPInstanceUID，统计分割覆盖率（默认端口 8000）
SEG_UID=$(python3 -c "
import json
job=json.load(open('job.json'))
print(next(f['sop_instance_uid'] for f in job['files'] if f['status']=='ok'))")

curl -f "http://localhost:8000/jobs/$JOB_ID/instances/$SEG_UID/seg-coverage" | python3 -m json.tool
```

```jsonc
{
  "sop_instance_uid": "2.25.3132…",
  "output_file": "2.25.3132….dcm",
  "segmentation_type": "BINARY",
  "rows": 4,
  "columns": 4,
  "number_of_frames": 4,
  "group_count": 3,
  "groups": [
    {"source_sop_instance_uid": "2.25.1528…", "source_output_file": "2.25.1528….dcm",
     "segment_number": 1, "frame_count": 2,
     "foreground_pixels": 2, "image_pixels": 16, "coverage": 0.125},
    {"source_sop_instance_uid": "2.25.1528…", "source_output_file": "2.25.1528….dcm",
     "segment_number": 2, "frame_count": 1,
     "foreground_pixels": 3, "image_pixels": 16, "coverage": 0.1875},
    {"source_sop_instance_uid": "2.25.3174…", "source_output_file": "2.25.3174….dcm",
     "segment_number": 1, "frame_count": 1,
     "foreground_pixels": 16, "image_pixels": 16, "coverage": 1.0}
  ]
}
```

出现以下任一情况时接口**不输出部分统计**，而是返回 `409`
（`detail.code = SEG_COVERAGE_CONFLICT`），在 `detail.problems[]` 中
**一次性列出全部问题**（每条都带匿名 SEG SOP UID，帧级问题附 `frame_index`，
源图像问题附匿名 `source_sop_instance_uid`，绝不回显原始 UID）：

| `reason` | 触发条件 |
| --- | --- |
| `unreadable` | SEG 匿名输出不可读（损坏 / 截断 / 编码错误） |
| `not_segmentation` | 实例不是 Segmentation Storage |
| `unsupported_segmentation_type` | `SegmentationType` 非 BINARY 或缺失（`segmentation_type` 给出实际值） |
| `unsupported_transfer_syntax` | 压缩/封装传输语法（`transfer_syntax_uid` 给出实际值） |
| `sop_instance_uid_mismatch` | 文件内 SOPInstanceUID 与请求的匿名 UID 不一致 |
| `invalid_pixel_metadata` | 非 1-bit 像素元数据（Rows/Columns 非法、BitsAllocated/BitsStored/HighBit 不为 1/1/0） |
| `invalid_number_of_frames` | `NumberOfFrames` 缺失或取值非法（非正整数） |
| `frame_count_mismatch` | `PerFrameFunctionalGroupsSequence` 缺失或条目数与声明帧数不符 |
| `missing_pixel_data` | 无 `PixelData` |
| `pixel_decode_failed` | 像素数据截断或与元数据不符，1-bit 掩码无法解码 |
| `missing_segment_number` | 帧缺少有效 `ReferencedSegmentNumber` |
| `missing_source_reference` | 帧缺少 `DerivationImageSequence`/`SourceImageSequence` 引用 |
| `multiple_source_references` | 同一帧在全部 `DerivationImageSequence`/`SourceImageSequence` 中引用了**两个及以上不同**匿名 SOP UID（多源分割；`frame_index` 给出帧号，`source_sop_instance_uids` 给出去重后的冲突 UID 列表）。此情况下无法唯一确定该帧归属的源图像，整份统计返回 409 且不输出部分结果，避免把帧静默归给首个引用而算出错误覆盖率 |
| `source_not_in_output` | 帧引用的源图像匿名 SOP UID 不在任务成功输出中（引用缺失） |
| `source_ambiguous_output` | 源图像匿名 SOP UID 对应多份输出（`output_files` 列出冲突文件，引用歧义） |
| `source_output_missing` | 源图像输出文件在任务目录中已不存在 |
| `source_unreadable` | 源图像不可读或缺少有效 `Rows/Columns` |
| `source_dimension_mismatch` | 源图像尺寸与掩码不一致（`source_rows`/`source_columns` 给出实际值） |

其余错误（`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任务 ID 不存在或已过期 |
| `404` | `INSTANCE_NOT_FOUND` | 该匿名 SOPInstanceUID 不在任务的成功输出中 |
| `404` | `INSTANCE_OUTPUT_MISSING` | 任务记录有该输出但磁盘文件已不存在（`reason=output_missing`） |
| `409` | `JOB_NOT_READY` | 任务 `status != ready`（匿名化处理失败） |
| `409` | `AMBIGUOUS_INSTANCE_OUTPUT` | 同一匿名 SOP UID 对应多份输出文件（`reason=ambiguous_output`，`output_files` 列出冲突文件） |

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/instances/{anon_seg_uid}/seg-coverage"
)
if resp.status_code == 404:
    print("任务或匿名 SEG 不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("无法统计覆盖率：", detail["code"])
    for p in detail.get("problems", []):
        # reason 见上表；帧级问题带 frame_index，源图像问题带 source_sop_instance_uid
        print(p["reason"], "frame:", p.get("frame_index"),
              "source:", p.get("source_sop_instance_uid"))
else:
    resp.raise_for_status()
    for g in resp.json()["groups"]:
        print(f"源 {g['source_sop_instance_uid']} 段 {g['segment_number']}: "
              f"{g['foreground_pixels']}/{g['image_pixels']} "
              f"= {g['coverage']:.2%}")
```

### 跨任务合并下载同一匿名检查（`POST /jobs/merged-study/download`）

当同一个检查的实例被拆到**多个批次 ZIP** 上传时（只要使用**同一把稳定密钥**，
匿名 UID 就可跨批次对齐），可用本接口一次性取回该检查的完整实例集合。数据**只来自
各已完成任务 output 目录中的匿名 DICOM 文件**，不会重新解压或处理原始 ZIP。

请求体（JSON）：

```json
{"job_ids": ["0d03…", "7f2a…", "a91f…"], "study_instance_uid": "2.25.8912…"}
```

- `job_ids`：**2～5 个**已完成（`status=ready`）任务 ID，不可重复、不可为空白，
  否则 `422`；
- `study_instance_uid`：**匿名后**的检查 UID，取自任一参与任务结果中的
  `studies[].new_study_instance_uid`（或某文件的 `study_instance_uid`）。

合并规则：

1. 从各任务成功输出中选出属于该匿名检查的全部实例作为初选集合；
2. 在**所有指定任务**的成功输出上建立全局匿名 SOPInstanceUID 索引，递归读取集合内
   匿名 DICOM **任意序列层级**的 `ReferencedSOPInstanceUID`，把**跨任务**被引用的
   实例（可属于其他匿名检查）补入集合，重复扫描直至闭包稳定；
3. **按匿名 SOPInstanceUID 去重**：同一 UID 的多份输出（跨任务重复上传，或单任务内
   重复 SOP 改名输出）**逐字节一致（SHA-256 相同）时只保留一份**；字节**不一致**则
   返回 `409`，在 `detail.conflicting_sop_instance_uids` 与
   `detail.problems[]`（`reason=byte_conflict`，附各份输出的任务 ID / 扁平文件名 /
   SHA-256）中列出全部冲突 UID；
4. 引用目标在**所有指定任务**的成功输出中均不存在时返回 `409`，在
   `detail.missing_sop_instance_uids` 与 `detail.problems[]`
   （`reason=missing_in_all_jobs`）中列出全部缺失的匿名 SOP UID；
5. 字节冲突与缺失引用会在**同一次 409 响应中一次性全部列出**，任何情况下都
   **不产生残缺 ZIP**；
6. 只接受**相同密钥指纹**的任务：指纹不一致直接 `409`
   （`SECRET_FINGERPRINT_MISMATCH`）；任一任务不存在/已过期返回 `404`，
   未完成返回 `409`；该匿名检查在所有指定任务的成功输出中都不存在时返回 `404`
   （`STUDY_NOT_FOUND`）。

成功时返回**扁平 ZIP**：条目名即匿名 `SOPInstanceUID.dcm`（单任务内重复 SOP 的
`<uid>-2.dcm` 改名输出也统一归并为 `<uid>.dcm`），**不含任何来源任务的目录结构、
原始路径或任务 ID**；响应头给出计数：

| 响应头 | 含义 |
| --- | --- |
| `X-Merged-Job-Count` | 参与合并的任务数（2～5） |
| `X-Merged-Study-Initial-File-Count` | 初选集合的匿名 SOP UID 数（跨任务去重后） |
| `X-Merged-Study-Added-File-Count` | 递归补入的跨任务/跨检查 SOP UID 数 |
| `X-Merged-Study-Deduped-Copy-Count` | 字节一致而被丢弃的重复物理副本数 |
| `X-Merged-Study-File-Count` | ZIP 总条目数（初选 + 补入，按 UID 去重） |

```bash
curl -X POST http://localhost:8000/jobs/merged-study/download \
  -H "Content-Type: application/json" \
  -d '{"job_ids": ["0d03abcd1234", "7f2a9876ef01"],
       "study_instance_uid": "2.25.8912123456789012345678901234567"}' \
  -OJ -D merged.headers
grep -i '^x-merged-' merged.headers
# x-merged-job-count: 2
# x-merged-study-initial-file-count: 3
# x-merged-study-added-file-count: 1
# x-merged-study-deduped-copy-count: 1
# x-merged-study-file-count: 4
```

Python 示例（含 404/409 处理）：

```python
resp = requests.post(
    "http://localhost:8000/jobs/merged-study/download",
    json={"job_ids": [job1_id, job2_id], "study_instance_uid": study_uid},
)
if resp.status_code == 404:
    print("任务或匿名检查不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("拒绝合并：", detail["code"])
    for p in detail["problems"]:
        # reason: byte_conflict（同 UID 多份输出字节不一致，p["copies"] 给出
        #       各份的 job_id / output_file / sha256）
        #       | missing_in_all_jobs（引用目标在所有指定任务中均不存在）
        print(p["sop_instance_uid"], p["reason"])
else:
    resp.raise_for_status()
    print("初选：", resp.headers["X-Merged-Study-Initial-File-Count"],
          "补入：", resp.headers["X-Merged-Study-Added-File-Count"],
          "去重副本：", resp.headers["X-Merged-Study-Deduped-Copy-Count"])
    open("merged-study.zip", "wb").write(resp.content)
```

错误响应（HTTP 4xx，`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任一任务 ID 不存在或已过期（`missing_job_ids`） |
| `404` | `STUDY_NOT_FOUND` | 该匿名 StudyInstanceUID 不在任一指定任务的成功输出中 |
| `409` | `JOBS_NOT_COMPLETED` | 存在 `status != ready` 的任务（`not_ready_jobs`） |
| `409` | `SECRET_FINGERPRINT_MISMATCH` | 任务间密钥指纹不一致（`fingerprints`） |
| `409` | `MERGED_REFERENCE_CLOSURE_INCOMPLETE` | 闭包无法在指定任务成功输出内闭合：`conflicting_sop_instance_uids`（字节冲突）与 `missing_sop_instance_uids`（引用缺失）同时给出，`problems[]` 逐项给出匿名 SOP UID、`reason`、来源任务与 SHA-256 |
| `422` | —（Pydantic 校验） | 任务数不在 2～5、重复/空白 ID，或 `study_instance_uid` 为空白 |

`409` 响应示例：

```jsonc
{
  "detail": {
    "code": "MERGED_REFERENCE_CLOSURE_INCOMPLETE",
    "message": "跨任务引用闭包无法在指定任务的成功输出内闭合（存在缺失引用或字节冲突），已拒绝输出合并 ZIP",
    "study_instance_uid": "2.25.8912…",
    "conflicting_sop_instance_uids": ["2.25.1613…"],
    "missing_sop_instance_uids": ["2.25.4242…"],
    "problems": [
      {"sop_instance_uid": "2.25.1613…",
       "reason": "byte_conflict",
       "job_ids": ["0d03…", "7f2a…"],
       "copies": [
         {"job_id": "0d03…", "output_file": "2.25.1613….dcm",
          "sha256": "a3f1…"},
         {"job_id": "7f2a…", "output_file": "2.25.1613….dcm",
          "sha256": "9c0b…"}
       ],
       "message": "同一匿名 SOPInstanceUID 在不同任务（或重复输出）中的文件字节不一致，无法确定唯一内容"},
      {"sop_instance_uid": "2.25.4242…",
       "reason": "missing_in_all_jobs",
       "job_ids": [],
       "copies": [],
       "message": "引用目标在所有指定任务的成功输出中均不存在"}
    ]
  }
}
```

> 注意：建议先用 `POST /jobs/consistency-check` 核对批次。同一密钥下匿名输出理论上
> 逐字节一致；`byte_conflict` 意味着参与批次的匿名化算法版本不一致或输出被篡改，
> 此时应停止合并并排查来源批次。合并 ZIP 为一次性临时文件，响应结束后即从磁盘删除。

错误响应（HTTP 4xx，`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任务 ID 不存在或已过期 |
| `404` | `STUDY_NOT_FOUND` | 该匿名 StudyInstanceUID 不在任务的成功输出中 |
| `409` | `JOB_NOT_READY` | 任务 `status != ready`（匿名化处理失败） |
| `409` | `REFERENCE_CLOSURE_INCOMPLETE` | 引用闭包无法在成功输出内闭合；`detail.problems[]` 逐项给出匿名 `sop_instance_uid`、`reason`（`not_in_output` / `ambiguous_output`）与 `output_files`（歧义时的冲突文件名） |

`409` 响应示例：

```jsonc
{
  "detail": {
    "code": "REFERENCE_CLOSURE_INCOMPLETE",
    "message": "引用闭包无法在本任务的成功输出内闭合，已拒绝输出子集 ZIP",
    "study_instance_uid": "2.25.8912…",
    "problems": [
      {"sop_instance_uid": "2.25.1613…",
       "reason": "not_in_output",          // 或 ambiguous_output
       "output_files": [],                  // ambiguous_output 时为冲突文件名列表
       "message": "引用目标不在本任务的成功输出中"}
    ]
  }
}
```

Python 示例：

```python
import requests

with open("studies.zip", "rb") as fh:
    resp = requests.post(
        "http://localhost:8000/jobs",
        files={"file": ("studies.zip", fh, "application/zip")},
        data={
            "secret": "my-stable-secret-2026",
            "shift_dates": "true",
            # 可选：自定义平移区间（须成对提供且 -365 <= min <= max <= 365）
            "date_shift_min": "-30",
            "date_shift_max": "30",
        },
    )
job = resp.json()
for f in job["files"]:
    print(f["name"], f["status"], [d["code"] for d in f["diagnostics"]])

requests.get(
    f"http://localhost:8000/jobs/{job['id']}/download"
).raise_for_status()
open("anonymized.zip", "wb").write(
    requests.get(f"http://localhost:8000/jobs/{job['id']}/download").content
)
```

---

## 跨任务一致性核对（`POST /jobs/consistency-check`）

分多个批次上传、但使用**同一把稳定密钥**时，确定性派生应保证：同一患者跨批次得到
相同伪名、同一原始 UID 跨批次映射到同一新 UID。本接口接收 **2～5 个已完成任务**
（`status=ready`），核对：

1. **secret 指纹**：所有任务必须来自同一密钥（SHA-256 前 12 位），否则直接 `409`；
2. **患者稳定伪名**：同一患者 key 的 `new_patient_id` / `new_patient_name` 是否一致、
   日期偏移是否一致（`shift_dates` 开关或平移区间不同会产生 `date_shift_mismatch`，
   各任务使用的 `date_shift_min/max` 在 `jobs[]` 中回显便于核对）。患者 key 按
   [身份归组规则](#患者身份归组规则patient_key)计算：非空患者号带非空签发机构时为
   `id:<签发机构>␟<患者号>`，因此同号不同机构的两名患者是两个不同的键，不会被
   误判为同一患者，也不会因各自只在一个批次出现而报伪名冲突；
3. **Study / Series / SOP 三级 UID 映射**：同一原始 UID 是否映射到同一新 UID，
   以及不同原始 UID 是否碰撞到同一新 UID。

请求体（JSON）：

```json
{"job_ids": ["0d03…", "7f2a…"]}
```

- 数组长度必须为 **2～5**，元素不可重复、不可为空白字符串，否则 `422`。

### 差异明细分页（`level` / `limit` / `cursor`）

当差异明细很多（例如跨大批次时 `only_in_one_job` 可能成千上万），可在请求中追加
`level` 对**单个层级**的差异明细做游标分页，响应会在顶层额外返回 `items` /
`next_cursor` / `has_more`；原有的完整计数（`*_count`、`summary`）与 `overall_status`
**始终完整给出，不受分页影响**。不传 `level` 时响应与旧版本完全一致（不出现任何
分页字段）。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | string | 否 | `patient` / `study` / `series` / `sop`；不传则返回旧版完整报告 |
| `limit` | integer | 否 | 每页条数，**1～200**（越界返回 `422`）；缺省 50 |
| `cursor` | string | 否 | 上一页响应中的不透明 `next_cursor`，首页不传 |

```json
{"job_ids": ["0d03…", "7f2a…"], "level": "sop", "limit": 100}
```

响应在原报告基础上新增三个顶层字段：

| 字段 | 说明 |
| --- | --- |
| `items` | 当前层级本页的差异明细，按**固定业务键**稳定排序 |
| `next_cursor` | 下一页不透明游标；`has_more=false` 时为 `null` |
| `has_more` | 该层级是否还有后续明细 |

排序规则（固定业务键，先类型后标识，均为字典序，与比对执行顺序无关）：

1. `conflict`（患者层含 `date_shift_mismatch`）→ `uid_new_collision` → `only_in_one_job`；
2. 同类型内：患者层按 `patient_key`；UID 层按 `old_uid`，碰撞条目按 `new_uid`。

游标安全语义（不透明、HMAC 签名，默认有效期 **15 分钟**）：

- 游标载荷绑定**任务集合**（排序后的 `job_ids`）与**筛选条件**（`level`）；
- 被**篡改**或格式非法、**过期**、或复用到**其他任务集合 / 其他 level** 时返回 `400`
  （`detail.code` 分别为 `INVALID_CURSOR` / `CURSOR_EXPIRED` / `CURSOR_SCOPE_MISMATCH`），
  此时应丢弃游标、从第一页重新开始；
- 同一任务集合以不同顺序请求视为相同作用域，游标可继续使用；
- 游标不含 `limit`，可用不同页长继续翻页；服务重启后进程级签名密钥轮换，旧游标
  按篡改处理（`400`）。

> 注意：报告内嵌的 `patient_mappings.differences` 与各层 `uid_mappings[].differences`
> 仍按原口径截断（每层最多 500 条，冲突/碰撞始终完整）；**完整明细通过分页接口获取**，
> 截断时 `differences_truncated=true`，计数始终完整。

翻页示例（Python）：

```python
job_ids = [job1_id, job2_id]
cursor = None
while True:
    body = {"job_ids": job_ids, "level": "sop", "limit": 200}
    if cursor:
        body["cursor"] = cursor
    resp = requests.post(
        "http://localhost:8000/jobs/consistency-check", json=body
    )
    if resp.status_code == 400:
        # 游标篡改/过期/跨条件复用：回退到第一页
        cursor = None
        continue
    resp.raise_for_status()
    page = resp.json()
    for item in page["items"]:
        print(item["difference_type"], item.get("old_uid") or item.get("new_uid"))
    if not page["has_more"]:
        break
    cursor = page["next_cursor"]
```

curl 示例：

```bash
curl -X POST http://localhost:8000/jobs/consistency-check \
  -H "Content-Type: application/json" \
  -d '{"job_ids": ["0d03abcd1234", "7f2a9876ef01"]}'
```

Python 示例：

```python
resp = requests.post(
    "http://localhost:8000/jobs/consistency-check",
    json={"job_ids": [job1_id, job2_id, job3_id]},
)
if resp.status_code == 404:
    print("任务不存在：", resp.json()["detail"]["missing_job_ids"])
elif resp.status_code == 409:
    print("无法核对：", resp.json()["detail"]["code"])  # 未完成或指纹不一致
else:
    report = resp.json()
    print(report["overall_status"])      # consistent | inconsistent
    print(report["summary"])             # 各类冲突/覆盖差异计数
```

响应示例：

```jsonc
{
  "overall_status": "consistent",       // consistent | inconsistent
  "secret_fingerprint": "3efffbab4a80",
  "jobs": [
    {"id": "0d03…", "status": "ready", "secret_fingerprint": "3efffbab4a80",
     "shift_dates": true, "date_shift_min": -365, "date_shift_max": 365,
     "source_zip": "batch-a.zip",
     "file_count": 6, "output_file_count": 4, "error_count": 2,
     "warning_count": 1, "patient_count": 2, "study_count": 2,
     "series_count": 3, "sop_count": 4,
     "created_at": 1790120660.8, "expires_at": 1790124260.8}
  ],
  "patient_mappings": {
    "checked_keys": 2,                  // >=2 个任务共同出现、实际比对过的患者数
    "conflict_count": 0,                // 同 key 派生出不同伪名
    "date_shift_mismatch_count": 0,     // 伪名一致但日期偏移不同
    "only_in_one_job_count": 0,         // 患者仅出现在部分任务（覆盖范围差异）
    "shift_dates_consistent": true,
    "differences": [
      {"patient_key": "id:P-002",
       "difference_type": "only_in_one_job",   // conflict | date_shift_mismatch | only_in_one_job
       "values": {"0d03…": {"new_patient_id": "MII3…",
                            "new_patient_name": "ANON^MII3RH4JPC",
                            "date_shift_days": -46}},
       "present_in": ["0d03…"], "missing_in": ["7f2a…"]}
    ]
  },
  "uid_mappings": [
    {"level": "study",                  // study | series | sop
     "checked_old_uids": 2,
     "conflict_count": 0,               // 同一原始 UID -> 不同新 UID
     "only_in_one_job_count": 0,        // UID 仅出现在部分任务（覆盖范围差异）
     "new_uid_collision_count": 0,      // 不同原始 UID 碰撞到同一新 UID
     "differences_truncated": false,    // 明细过多时截断（计数始终完整）
     "differences": [
       {"level": "sop", "old_uid": "1.2.840.…",
        "difference_type": "conflict",  // conflict | only_in_one_job | uid_new_collision
        "mappings": {"0d03…": "2.25.111…", "7f2a…": "2.25.222…"},
        "present_in": ["0d03…", "7f2a…"], "missing_in": []}
     ]}
  ],
  "summary": {
    "patient_conflict": 0,
    "patient_date_shift_mismatch": 0,
    "uid_conflict": 0,
    "uid_new_collision": 0,
    "patient_only_in_one_job": 0,
    "uid_only_in_one_job": 0
  }
  // 请求带 level 时，顶层还会出现：
  // "items": [ {"level": "sop", "old_uid": "1.2.840.…",
  //             "difference_type": "only_in_one_job",
  //             "mappings": {"0d03…": "2.25.111…"},
  //             "present_in": ["0d03…"], "missing_in": ["7f2a…"]} ],
  // "next_cursor": "eyJ2Ijoi…（不透明，has_more=false 时为 null）",
  // "has_more": true
}
```

判定口径：

- `overall_status = "inconsistent"` 当且仅当存在患者伪名冲突、日期偏移不一致、
  UID 映射冲突或 UID 碰撞；仅出现在部分任务中的患者/UID 属于**批次覆盖范围差异**
  （`only_in_one_job`），不计为冲突；
- 同一密钥下确定性派生理论上不会产生 `conflict` / `uid_new_collision`，出现这类结果
  意味着派生算法版本不一致或数据被篡改，应停止合并这些批次；
- 差异明细每层最多内嵌返回 500 条（冲突/碰撞始终完整保留），超出时
  `differences_truncated=true`，但各类 `*_count` 计数仍然完整；需要完整明细时
  使用 `level` + `limit` + `cursor` 分页参数逐层获取。

错误响应（HTTP 4xx，`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `400` | `INVALID_CURSOR` | 分页游标格式/签名非法、被篡改，或服务重启后旧游标失效 |
| `400` | `CURSOR_EXPIRED` | 分页游标超过有效期（默认 15 分钟，需从第一页重新开始） |
| `400` | `CURSOR_SCOPE_MISMATCH` | 游标复用到其他任务集合或其他 `level` |
| `404` | `JOB_NOT_FOUND` | 任一任务 ID 不存在或已过期（`missing_job_ids` 给出缺失列表） |
| `409` | `JOBS_NOT_COMPLETED` | 存在 `status != ready` 的任务（失败/未完成，见 `not_ready_jobs`） |
| `409` | `SECRET_FINGERPRINT_MISMATCH` | 任务间密钥指纹不一致（`fingerprints` 给出各任务指纹） |
| `422` | —（Pydantic 校验） | ID 数量不在 2～5、重复、空白，`level` 非法，`limit` 不在 1～200，或请求体格式错误 |

`POST /jobs` 表单本身的 `422` 还包括：密钥过短、非 `.zip`、空上传、`shift_dates`
非布尔值、`date_shift_min/max` 非整数、超出 -365..365、或两者只提供其一 /
`min > max`。

---

## 响应结构（标签变更预览与诊断）

`POST /jobs` / `GET /jobs/{id}` 返回：

```jsonc
{
  "id": "0d03…",
  "status": "ready",                 // ready | failed
  "secret_fingerprint": "ab12…",     // 密钥 SHA-256 前 12 位，不回显密钥本身
  "shift_dates": true,
  "date_shift_min": -365,            // 本任务实际使用的日期平移闭区间
  "date_shift_max": 365,
  "file_count": 6,
  "output_file_count": 4,            // 实际写入匿名 ZIP 的文件数
  "error_count": 2,
  "warning_count": 1,
  "patients": [
    {"key": "id:P-001", "new_patient_id": "MII3RH4JPCRSHQ4D",
     "new_patient_name": "ANON^MII3RH4JPC", "date_shift_days": -46,
     "file_count": 3}
  ],
  "studies":  [ /* old/new StudyInstanceUID 映射 */ ],
  "series":   [ /* old/new SeriesInstanceUID 映射 */ ],
  "files": [
    {
      "name": "patient_1/rt/RS0001.dcm",
      "entry_index": 2,              // ZIP 条目序号（0 基）；同名条目以此区分
      "status": "warning",           // ok | warning | error | skipped
      "output_name": "2.25.9391….dcm",
      "study_instance_uid":  "2.25.…",
      "series_instance_uid": "2.25.…",
      "sop_instance_uid":    "2.25.…",
      "diagnostics": [
        {"code": "DANGLING_REFERENCE", "severity": "warning",
         "message": "ReferencedSOPSequence[0].ReferencedSOPInstanceUID 指向的 SOP 实例不在压缩包内：…（仍会被一致重写）"}
      ],
      "changes": [
        {"path": "ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0].ContourImageSequence[0]",
         "tag": "(0008,1155)", "keyword": "ReferencedSOPInstanceUID",
         "action": "REPLACE", "old": "1.2.840.…", "new": "2.25.…"},
        {"tag": "(0010,0020)", "keyword": "PatientID",
         "action": "REPLACE", "old": "P-001", "new": "MII3RH4JPCRSHQ4D"},
        {"tag": "(0008,0050)", "keyword": "AccessionNumber",
         "action": "EMPTY", "old": "ACC001", "new": null},
        {"tag": "(0010,1040)", "keyword": "PatientAddress",
         "action": "REMOVE", "old": "1 Secret Street", "new": null}
      ],
      "changes_truncated": false
    }
  ]
}
```

`action` 含义：`REPLACE`（确定性替换）、`EMPTY`（置空保留标签）、
`REMOVE`（整标签删除）。嵌套序列中的标签用点号路径标注，例如
`ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0]…`。
`name` 为 ZIP 原始成员名，`entry_index` 为条目在中央目录中的 0 基序号；
同名条目（含 `./` 等规范化后同址的名称）各自独立处理，见
[ZIP 同名条目与重复实例](#zip-同名条目与重复实例)。

### 诊断码

| code | 严重级别 | 含义 |
| --- | --- | --- |
| `INVALID_DICOM` | error | 文件不是合法 DICOM（缺少 File Meta / 魔数不符） |
| `READ_ERROR` | error | 文件截断或标签编码损坏，无法解析 |
| `WRITE_ERROR` | error | 匿名化写出阶段异常 |
| `DANGLING_REFERENCE` | warning | 序列引用的 SOP 实例不在 ZIP 内（引用仍一致重写） |
| `MISSING_STUDY_UID` / `MISSING_SERIES_UID` / `MISSING_SOP_UID` | warning | 顶层 UID **标签缺失或标签存在但值为空（含仅空白）**，已生成确定性替代 UID |
| `INVALID_DATE` | warning | DA/DT 值无法解析（多值时任一项非法），已清空整个标签 |
| `PATIENT_IDENTITY_FALLBACK` | warning | 缺少 PatientID/Name，按 Study UID 或文件内容归组 |
| `DUPLICATE_SOP_INSTANCE` | warning | 多个文件共享同一 SOP UID，输出文件自动改名 |
| `DICOMDIR_SKIPPED` | info | DICOMDIR 不写入匿名包（其路径会泄露 PHI） |

出错文件不会进入匿名 ZIP；`X-Job-Error-Count` 响应头给出错误文件数。任务结果
默认保留 1 小时（可配置），到期自动清理磁盘；未过期任务在服务重启后从工作卷
恢复（见[任务持久化与重启恢复](#任务持久化与重启恢复)）。

---

## 匿名化策略说明

**身份字段**

- 患者按[身份归组规则](#患者身份归组规则patient_key)确定 `patient_key`：非空
  `PatientID` 带非空 `IssuerOfPatientID` 时以签发机构 + 患者号共同确定身份
  （`id:<签发机构>␟<患者号>`），不同机构复用同号不合并；签发机构为空时沿用
  `id:<患者号>`，与旧版本结果兼容；无患者号时按 PatientName / StudyInstanceUID /
  文件哈希逐级回退。
- `PatientID`、`PatientName`：替换为患者级稳定伪名（Base32 编码的 HMAC 令牌 /
  `ANON^xxxxxx`）；嵌套序列中的患者标识同步替换。
- `IssuerOfPatientID` 整标签删除（不保留原值），
  `IssuerOfPatientIDQualifiersSequence`、`OtherPatientIDs(Sequence)` 等关联身份
  字段一并删除。
- `PatientBirthDate`：开启日期平移时按该患者偏移平移，否则清空。
- 其余全部 `DA`/`DT`（含嵌套序列、多值及反斜杠分隔值）：开启平移时逐项按
  该患者偏移平移（保留项数、顺序与 `DT` 的时间/时区尾部）；任一项非法即清空
  整个标签并记 `INVALID_DATE`；关闭平移时保持原值不动。
- 其余所有人名（VR=`PN`）一律置空。
- Annex E 列举的机构、站点、设备序列号、申请单号、自由文本/备注等标签：
  置空或删除（完整列表见 `app/tags.py`）。
- **全部私有标签**（odd group）删除。
- 写入审计标签：`PatientIdentityRemoved = YES`、
  `DeidentificationMethod = "DICOM-ANON-API; PS3.15 BASIC APPLICATION CONFIDENTIALITY PROFILE"`。

**UID 重写（关系保持）**

- 新 UID 形态为 `2.25.<128bit 整数>`（DICOM 规定的 ISO 9834 整数弧，无需注册根）。
- 映射由 `HMAC-SHA256(secret, "uid" | 原始UID)` 确定性得到；处理前先做两遍扫描：
  第一遍收集包内全部顶层与嵌套 UID，第二遍统一重写，保证无论引用出现在
  `ReferencedSOPSequence`、RT 的 `ContourImageSequence`、
  `ReferencedFrameOfReferenceSequence` 等任何深度，都与被引用实例的新 UID 一致。
- `SOPClassUID`、`TransferSyntaxUID` 等**类型性 UID 不重写**（重写会破坏文件语义）；
  文件元信息 `MediaStorageSOPInstanceUID` 与顶层 `SOPInstanceUID` 保持一致。
- 匿名 ZIP 内文件按**新 SOPInstanceUID 扁平命名**，不含原始路径。

**顶层 UID 标签存在但值为空（边界说明）**

部分设备导出的 DICOM 会带上 `StudyInstanceUID` / `SeriesInstanceUID` /
`SOPInstanceUID` 标签却把值留空（或只含空白）。服务**一律按标签值**判断这三类
UID 是否缺失——“标签存在但为空/空白”与“标签完全缺失”同口径处理：

1. 第二遍写出前先对每个文件按既有确定性规则补齐：以
   `HMAC-SHA256(secret, "missing:<层级>:<文件内容哈希>")` 生成该文件专属的替代
   UID（种子含文件内容，因此同输入跨批次稳定、不同文件互不相同），随后再与正常
   UID 一样进入全局重写；逐文件结果保留 `MISSING_STUDY_UID` /
   `MISSING_SERIES_UID` / `MISSING_SOP_UID` 警告，不会静默处理。
2. 补齐后匿名文件的顶层 Study/Series/SOP UID、File Meta 的
   `MediaStorageSOPInstanceUID`、任务结果中的检查/系列/实例索引与下载 ZIP 的
   扁平文件名（`<SOPInstanceUID>.dcm`）四处一致，不再出现 `WRITE_ERROR` 或输出
   空 UID 文件。
3. 同一批文件混合包含**空值、缺失与正常 UID** 时：正常 UID 的原有映射及任意深度
   的嵌套引用（`ReferencedSOPSequence`、RT `ContourImageSequence` 等）保持不变；
   仅缺失/空值项按上条补齐并给出逐文件 `MISSING_*` 诊断。

**安全**

- 解压前校验 ZIP 条目路径（拒绝 `../` 穿越、绝对路径、符号链接/设备节点）。
- 每个 ZIP 条目解压到以条目序号命名的独立暂存文件：同名（或 `./` 等规范化后
  同址）条目互不覆盖，两遍扫描与匿名写出均按条目读取各自字节。
- 解压总大小、条目数量设上限，防止 zip 炸弹；上传大小流式限制（超限返回 413）。
- 容器以非 root 用户运行；任务数据放在独立 Docker volume。

**局限与注意**

- 本服务实现的是 Basic Application Confidentiality Profile + 稳定 UID/伪名 +
  日期平移；**不做像素内刻录文字（burned-in PHI）的检测与擦除**。若来源设备会把
  姓名/编号刻录进图像，请先在采集端关闭或使用专用 OCR 擦除流程。
- 不重写封装像素数据传输语法，图像字节原样保留。
- 匿名化后建议在导入 PACS 前用抽样复核。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DICOM_ANON_PORT`（仅 Compose） | `8000` | 宿主机映射端口 |
| `DICOM_ANON_WORK_DIR` | `/data/dicom-anon` | 任务工作目录 |
| `DICOM_ANON_MAX_UPLOAD_BYTES` | `536870912`（512 MiB） | 上传 ZIP 上限 |
| `DICOM_ANON_MAX_EXTRACTED_BYTES` | `2147483648`（2 GiB） | 解压总量上限 |
| `DICOM_ANON_MAX_EXTRACTED_FILES` | `20000` | 解压文件数上限 |
| `DICOM_ANON_JOB_TTL_SECONDS` | `3600` | 任务结果保留时长 |
| `DICOM_ANON_MIN_SECRET_LENGTH` | `8` | 稳定密钥最小长度 |
| `DICOM_ANON_CURSOR_TTL_SECONDS` | `900` | 一致性核对分页游标的有效期（秒） |

---

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（/jobs、/jobs/consistency-check、/jobs/merged-study/download、全量/检查子集下载、系列几何、系列 KVP 分布、实例 PNG 预览、剂量 SR 照射事件、SEG 分割覆盖率、/health）
│   ├── anonymizer.py    # 逐条目独立暂存的安全解压 + 两遍扫描引擎：UID 映射、标签重写、递归序列、诊断、ZIP 打包
│   ├── preview.py       # 按匿名 SOP UID 从成功输出渲染单帧 8 位灰度 PNG：Rescale→VOI 窗/极值拉伸→MONOCHROME1 反相
│   ├── dose_events.py   # 按匿名 SOP UID 解析 X-Ray Radiation Dose SR：CT 照射事件容器边界、逐事件 CTDIvol/DLP 与事件 DLP 合计
│   ├── seg_coverage.py  # 按匿名 SOP UID 统计未压缩 BINARY SEG 覆盖率：逐帧源图像关联、同（源图像, 段号）1-bit 掩码并集
│   ├── dicomdir.py      # 从匿名输出重建 DICOMDIR 四级层级、稳定短 File ID、引用校验与阅片 ZIP
│   ├── geometry.py      # 系列单帧切片几何：方向余弦叉积法线、投影排序、中位数层间距与 >1.5× 缺口标记
│   ├── kvp.py           # 系列 CT 管电压逐帧分布：逐帧/共享功能组/顶层三级回退、各电压帧数与缺失帧数统计
│   ├── study_subset.py  # 按匿名 StudyInstanceUID 的引用闭包解析与子集 ZIP（复用匿名输出）
│   ├── merged_study.py  # 跨 2～5 个同密钥任务的检查合并：跨任务引用闭包、字节去重/冲突/缺失 409
│   ├── consistency.py   # 跨任务一致性核对：密钥指纹/患者伪名/UID 映射比对与差异明细分页
│   ├── pagination.py    # 差异明细不透明分页游标：HMAC 签名、绑定任务集合/level、TTL
│   ├── crypto.py        # HMAC 确定性派生：伪名、UID、日期偏移
│   ├── tags.py          # Annex E 标签策略与参与一致性重写的 UID 标签集合
│   ├── jobs.py          # 任务存储、线程池处理、TTL 清理、版本化清单持久化与重启恢复
│   ├── schemas.py       # 请求/响应模型（含一致性核对、分页、子集/合并/DICOMDIR/预览/几何/KVP/剂量事件/SEG 覆盖率错误模型）
│   └── config.py        # 环境变量配置
├── tests/               # pytest：加密派生 / 引擎 / API 端到端 / 一致性核对 / 分页游标 / 检查子集 / 跨任务合并 / DICOMDIR / PNG 预览 / 系列几何 / 系列 KVP 分布 / 剂量 SR 照射事件 / SEG 分割覆盖率 / ZIP 同名条目 / 清单持久化与重启恢复（349 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest -q
```
