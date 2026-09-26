"""API 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_serializer


class DiagnosticOut(BaseModel):
    code: str
    severity: str = Field(description="info | warning | error")
    message: str


class ChangeOut(BaseModel):
    path: str = ""
    tag: Optional[str] = None
    keyword: str
    action: str = Field(description="REPLACE | EMPTY | REMOVE")
    old: Optional[str] = None
    new: Optional[str] = None


class FileOut(BaseModel):
    name: str
    entry_index: int = Field(
        description="ZIP 条目序号（0 基，按中央目录顺序）；同名条目以此区分"
    )
    status: str = Field(description="ok | warning | error | skipped")
    output_name: Optional[str] = None
    patient_key: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    max_severity: Optional[str] = None
    diagnostics: list[DiagnosticOut] = []
    changes: list[ChangeOut] = []
    changes_truncated: bool = False


class PatientOut(BaseModel):
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int


class StudyOut(BaseModel):
    old_study_instance_uid: str
    new_study_instance_uid: str
    patient_key: str
    file_count: int


class SeriesOut(BaseModel):
    old_series_instance_uid: str
    new_series_instance_uid: str
    new_study_instance_uid: str
    file_count: int


class JobOut(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    date_shift_min: int = Field(
        default=-365,
        ge=-365,
        le=365,
        description="本任务日期平移使用的闭区间下界（与 date_shift_max 成对）",
    )
    date_shift_max: int = Field(
        default=365,
        ge=-365,
        le=365,
        description="本任务日期平移使用的闭区间上界（与 date_shift_min 成对）",
    )
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    changes_capped: bool
    patients: list[PatientOut]
    studies: list[StudyOut]
    series: list[SeriesOut]
    files: list[FileOut]
    error: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    status: str
    source_zip: str
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    created_at: float
    expires_at: float


# --------------------------------------------------------------------------- #
# 跨任务一致性核对（POST /jobs/consistency-check）
# --------------------------------------------------------------------------- #
class ConsistencyCheckRequest(BaseModel):
    job_ids: list[str] = Field(
        description="已完成（status=ready）任务 ID，2～5 个；不得重复",
        min_length=2,
        max_length=5,
    )
    level: Optional[Literal["patient", "study", "series", "sop"]] = Field(
        default=None,
        description=(
            "差异明细分页层级。不传：返回原完整计数报告（向后兼容）；"
            "传入时在顶层额外返回该层级按固定业务键排序的 items / next_cursor / has_more，"
            "报告内的完整 *_count 与 overall_status 仍然给出。"
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=200,
        description="每页差异明细条数，1～200；level 存在且未传时使用服务端默认值 50",
    )
    cursor: Optional[str] = Field(
        default=None,
        description=(
            "上一页响应返回的不透明 next_cursor；游标绑定本次任务集合与 level，"
            "篡改、过期或跨任务集合/层级复用将返回 400"
        ),
    )

    @field_validator("job_ids")
    @classmethod
    def _strip_and_reject_duplicates(cls, value: list[str]) -> list[str]:
        ids = [str(jid).strip() for jid in value]
        if any(not jid for jid in ids):
            raise ValueError("任务 ID 不能为空字符串")
        if len(set(ids)) != len(ids):
            raise ValueError("任务 ID 不得重复")
        return ids


class ConsistencyJobSummary(BaseModel):
    id: str
    status: str = Field(description="固定为 ready（未完成任务会直接返回 409）")
    secret_fingerprint: str
    shift_dates: bool
    date_shift_min: int = Field(default=-365, ge=-365, le=365)
    date_shift_max: int = Field(default=365, ge=-365, le=365)
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    patient_count: int
    study_count: int
    series_count: int
    sop_count: int


class PatientMappingValue(BaseModel):
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int


class PatientMappingDifference(BaseModel):
    patient_key: str
    difference_type: str = Field(
        description="conflict（同 key 在不同任务派生出不同伪名） | "
        "date_shift_mismatch（伪名一致但日期偏移不同，通常因 shift_dates 开关不同） | "
        "only_in_one_job（该患者仅出现在部分任务中，属于覆盖范围差异，不是冲突）"
    )
    values: dict[str, PatientMappingValue] = Field(
        description="任务 ID -> 该任务下的稳定伪名/日期偏移（仅包含出现该患者的任务）"
    )
    present_in: list[str] = Field(description="出现该患者的任务 ID")
    missing_in: list[str] = Field(default=[], description="未出现该患者的任务 ID")


class PatientMappingReport(BaseModel):
    checked_keys: int = Field(description="在 >=2 个任务中共同出现、实际比对过的患者 key 数")
    conflict_count: int
    date_shift_mismatch_count: int
    only_in_one_job_count: int
    differences: list[PatientMappingDifference]
    shift_dates_consistent: bool = Field(
        description="所有参与任务的 shift_dates 开关是否一致（开关不同会导致日期偏移策略不一致）"
    )


class UidMappingDifference(BaseModel):
    level: str = Field(description="study | series | sop")
    old_uid: Optional[str] = Field(
        default=None, description="原始 UID；uid_new_collision 时可能为空"
    )
    difference_type: str = Field(
        description="conflict（同一原始 UID 映射到不同新 UID） | "
        "only_in_one_job（覆盖范围差异） | "
        "uid_new_collision（同一新 UID 在不同任务中对应不同原始 UID）"
    )
    mappings: dict[str, str] = Field(
        default={}, description="任务 ID -> 新 UID（conflict / only_in_one_job 时给出）"
    )
    new_uid: Optional[str] = Field(default=None, description="uid_new_collision 时的新 UID")
    sources: dict[str, str] = Field(
        default={}, description="uid_new_collision 时：任务 ID -> 各自的原始 UID"
    )
    present_in: list[str] = Field(default=[])
    missing_in: list[str] = Field(default=[])


class UidLevelReport(BaseModel):
    level: str = Field(description="study | series | sop")
    checked_old_uids: int = Field(description="在 >=2 个任务中共同出现、实际比对过的原始 UID 数")
    conflict_count: int
    only_in_one_job_count: int
    new_uid_collision_count: int
    differences_truncated: bool = Field(
        description="差异条目过多时已截断（计数仍然完整）"
    )
    differences: list[UidMappingDifference]


class ConsistencyDifferenceItem(BaseModel):
    """统一的差异明细条目（患者层 / UID 层），按类型只填充对应字段。"""
    level: str = Field(description="patient | study | series | sop")
    difference_type: str = Field(
        description="患者层：conflict | date_shift_mismatch | only_in_one_job；"
        "UID 层：conflict | only_in_one_job | uid_new_collision"
    )
    patient_key: Optional[str] = Field(default=None, description="患者层条目的业务键")
    old_uid: Optional[str] = Field(
        default=None, description="UID 层原始 UID；uid_new_collision 时为空"
    )
    new_uid: Optional[str] = Field(default=None, description="uid_new_collision 时的新 UID")
    values: Optional[dict[str, PatientMappingValue]] = Field(
        default=None, description="患者层：任务 ID -> 该任务下的稳定伪名/日期偏移"
    )
    mappings: Optional[dict[str, str]] = Field(
        default=None, description="UID 层 conflict / only_in_one_job：任务 ID -> 新 UID"
    )
    sources: Optional[dict[str, str]] = Field(
        default=None, description="uid_new_collision：任务 ID -> 各自的原始 UID"
    )
    present_in: list[str] = []
    missing_in: list[str] = []


class ConsistencyCheckOut(BaseModel):
    overall_status: str = Field(
        description="consistent（无任何冲突） | inconsistent（存在伪名或 UID 冲突）"
    )
    secret_fingerprint: str = Field(description="参与任务共同的密钥指纹（SHA-256 前 12 位）")
    jobs: list[ConsistencyJobSummary]
    patient_mappings: PatientMappingReport
    uid_mappings: list[UidLevelReport] = Field(
        description="按 Study / Series / SOP 三个层级给出的映射差异"
    )
    summary: dict[str, int] = Field(
        description="patient_conflict / patient_date_shift_mismatch / "
        "uid_conflict / uid_new_collision / "
        "patient_only_in_one_job / uid_only_in_one_job 计数"
    )
    # 仅当请求传入 level 时出现（model_serializer 会在未分页时剔除这三个键）
    items: Optional[list[ConsistencyDifferenceItem]] = Field(
        default=None,
        description="请求 level 对应层级的差异明细，按固定业务键排序（见层级内排序规则）",
    )
    next_cursor: Optional[str] = Field(
        default=None,
        description="下一页不透明游标；has_more=false 时为 null。游标绑定任务集合与 level",
    )
    has_more: Optional[bool] = Field(
        default=None, description="该层级是否还有后续差异明细"
    )

    @model_serializer(mode="plain")
    def _serialize(self):
        data = {
            "overall_status": self.overall_status,
            "secret_fingerprint": self.secret_fingerprint,
            "jobs": [j.model_dump() for j in self.jobs],
            "patient_mappings": self.patient_mappings.model_dump(),
            "uid_mappings": [u.model_dump() for u in self.uid_mappings],
            "summary": self.summary,
        }
        # 未请求分页（level 缺省）时保持与旧版响应完全一致，不输出分页键
        if self.items is not None or self.next_cursor is not None or self.has_more is not None:
            data["items"] = (
                [i.model_dump(exclude_none=True) for i in self.items]
                if self.items is not None else None
            )
            data["next_cursor"] = self.next_cursor
            data["has_more"] = self.has_more
        return data


class ConsistencyErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND | JOBS_NOT_COMPLETED | SECRET_FINGERPRINT_MISMATCH"
    )
    message: str
    missing_job_ids: list[str] = []
    not_ready_jobs: list[dict[str, str]] = []
    fingerprints: dict[str, str] = {}


class CursorErrorDetail(BaseModel):
    code: str = Field(
        description=(
            "INVALID_CURSOR（游标格式/签名非法、已被篡改，或服务重启后旧游标失效） | "
            "CURSOR_EXPIRED（游标超过有效期） | "
            "CURSOR_SCOPE_MISMATCH（游标被复用到其他任务集合或其他 level）"
        )
    )
    message: str


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# 按匿名 StudyInstanceUID 下载检查子集（GET /jobs/{id}/studies/{uid}/download）
# --------------------------------------------------------------------------- #
class StudySubsetProblemItem(BaseModel):
    sop_instance_uid: str = Field(description="出问题的匿名（已重写）SOPInstanceUID")
    reason: str = Field(
        description="not_in_output（引用目标不在本任务成功输出中） | "
        "ambiguous_output（同一匿名 SOP UID 对应多个输出文件） | "
        "output_missing（任务成功输出记录存在，但 output_dir 中物理文件已丢失）"
    )
    output_files: list[str] = Field(
        default=[],
        description="ambiguous_output 时给出相互冲突的扁平输出文件名；"
        "output_missing 时给出已丢失的扁平输出文件名",
    )
    message: str


class StudySubsetErrorDetail(BaseModel):
    code: str = Field(
        description="STUDY_NOT_FOUND（404） | REFERENCE_CLOSURE_INCOMPLETE（409） | "
        "JOB_NOT_READY（409） | ANONYMIZED_OUTPUT_UNREADABLE（500）"
    )
    message: str
    study_instance_uid: Optional[str] = None
    problems: list[StudySubsetProblemItem] = []


# --------------------------------------------------------------------------- #
# 实例影像 PNG 预览（GET /jobs/{id}/instances/{uid}/preview.png）
# --------------------------------------------------------------------------- #
class PreviewErrorDetail(BaseModel):
    code: str = Field(
        description="INSTANCE_NOT_FOUND（404，匿名 SOP UID 不在任务成功输出中） | "
        "INSTANCE_OUTPUT_MISSING（404，任务记录有该输出但磁盘文件已不存在） | "
        "JOB_NOT_READY（409，任务处理失败） | "
        "AMBIGUOUS_INSTANCE_OUTPUT（409，同一匿名 SOP UID 对应多份输出） | "
        "PREVIEW_UNSUPPORTED_IMAGE（422，无法解码或影像类型不支持）"
    )
    message: str
    sop_instance_uid: Optional[str] = None
    reason: Optional[str] = Field(
        default=None,
        description=(
            "PREVIEW_UNSUPPORTED_IMAGE 的机器可读细分原因："
            "unreadable | unsupported_transfer_syntax | unsupported_samples_per_pixel | "
            "unsupported_photometric_interpretation | multiframe | unsupported_bit_depth | "
            "invalid_pixel_metadata | missing_pixel_data | pixel_decode_failed | "
            "invalid_rescale | invalid_voi_window | sop_instance_uid_mismatch；"
            "AMBIGUOUS_INSTANCE_OUTPUT 时为 ambiguous_output；"
            "INSTANCE_OUTPUT_MISSING 时为 output_missing"
        ),
    )
    output_file: Optional[str] = Field(
        default=None, description="可定位时给出任务 output 目录内的扁平文件名"
    )
    output_files: list[str] = Field(
        default=[], description="reason=ambiguous_output 时给出相互冲突的输出文件名"
    )
    transfer_syntax_uid: Optional[str] = Field(
        default=None, description="reason=unsupported_transfer_syntax 时回传实际传输语法 UID"
    )


# --------------------------------------------------------------------------- #
# DICOMDIR 离线阅片包下载（GET /jobs/{id}/dicomdir/download）
# --------------------------------------------------------------------------- #
class DicomDirProblemItem(BaseModel):
    output_file: str = Field(
        description="出问题的扁平匿名输出文件名（任务 output 目录内的文件名）"
    )
    sop_instance_uid: Optional[str] = Field(
        default=None, description="可读取时给出文件内的匿名 SOPInstanceUID"
    )
    reason: str = Field(
        description="unreadable（匿名输出不可读/不是合法 DICOM） | "
        "missing_required_tags（缺少建立 DICOMDIR 必需标签） | "
        "ambiguous_output（同一匿名 SOP UID 对应多份输出） | "
        "output_missing（匿名输出文件在任务目录中不存在）"
    )
    missing_tags: list[str] = Field(
        default=[], description="reason=missing_required_tags 时给出缺失的标签关键字"
    )
    output_files: list[str] = Field(
        default=[], description="reason=ambiguous_output 时给出相互冲突的输出文件名"
    )
    message: str


class DicomDirErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_READY（409） | NO_ANONYMIZED_OUTPUT（409） | "
        "DICOMDIR_BUILD_INCOMPLETE（409） | "
        "DICOMDIR_WRITE_FAILED / DICOMDIR_REFERENCE_MISSING（500）"
    )
    message: str
    problems: list[DicomDirProblemItem] = []


class GeometrySliceItem(BaseModel):
    index: int = Field(description="按法线投影升序排列后的 0 基序号")
    sop_instance_uid: str = Field(description="该切片的匿名 SOPInstanceUID")
    projection: float = Field(
        description="ImagePositionPatient 在平面单位法线上的标量投影（毫米）"
    )
    image_position_patient: list[float] = Field(
        description="匿名文件中的 ImagePositionPatient（x/y/z，毫米）"
    )


class GeometrySpacingItem(BaseModel):
    index: int = Field(
        description="相邻间隙的 0 基序号（spacings[i] 连接 slices[i] 与 slices[i+1]）"
    )
    from_sop_instance_uid: str
    to_sop_instance_uid: str
    distance: float = Field(description="相邻切片沿法线的投影距离（毫米，恒为非负）")
    anomalous: bool = Field(description="该距离是否大于 1.5 × 中位数间距")


class GeometryExcludedItem(BaseModel):
    sop_instance_uid: str = Field(description="被排除实例的匿名 SOPInstanceUID")
    output_file: Optional[str] = Field(
        default=None, description="任务 output 目录内的扁平文件名"
    )
    reason: str = Field(description="multiframe（NumberOfFrames 显式 >1 或非法）")
    number_of_frames: Optional[str] = Field(
        default=None, description="文件中 NumberOfFrames 的原始文本"
    )
    message: str


class SeriesGeometryOut(BaseModel):
    series_instance_uid: str = Field(description="请求的匿名 SeriesInstanceUID")
    frame_of_reference_uid: str = Field(
        description="系列内统一的匿名 FrameOfReferenceUID"
    )
    image_orientation_patient: list[float] = Field(
        description="系列基准方向：行/列方向余弦（6 分量，单位向量）"
    )
    normal: list[float] = Field(
        description="行方向 × 列方向叉积得到的单位法线（3 分量）"
    )
    instance_count: int = Field(description="参与排序的有效单帧实例数")
    gap_check: str = Field(
        description="ok（已完成中位数缺口判定） | "
        "insufficient_slices（有效单帧实例不足 3 张，无法判断间隙）"
    )
    median_spacing: Optional[float] = Field(
        default=None,
        description="相邻投影距离的中位数（毫米）；insufficient_slices 时为 null",
    )
    gap_factor: float = Field(description="缺口阈值倍数，固定 1.5")
    anomalous_gap_indices: list[int] = Field(
        description="大于 1.5 × 中位数间距的相邻间隙序号（spacings[].index）"
    )
    slices: list[GeometrySliceItem] = Field(
        description="按法线投影升序排列的匿名 SOP UID 与投影坐标"
    )
    spacings: list[GeometrySpacingItem] = Field(
        description="排序后逐对相邻切片的间距（slices[i] -> slices[i+1]）"
    )
    message: Optional[str] = Field(
        default=None, description="insufficient_slices 时给出无法判断间隙的说明"
    )
    excluded_instances: list[GeometryExcludedItem] = Field(
        default=[],
        description="系列成功输出中未参与几何分析的实例（多帧实例）",
    )


class GeometryProblemItem(BaseModel):
    sop_instance_uid: Optional[str] = Field(
        default=None, description="出问题实例的匿名 SOPInstanceUID（歧义时为该共同 UID）"
    )
    sop_instance_uids: list[str] = Field(
        default=[],
        description=(
            "frame_of_reference_mismatch / orientation_mismatch / "
            "duplicate_projection 时给出同组全部匿名 SOP UID"
        ),
    )
    output_file: Optional[str] = Field(
        default=None, description="可定位时给出任务 output 目录内的扁平文件名"
    )
    output_files: list[str] = Field(
        default=[], description="ambiguous_output 时给出相互冲突的输出文件名"
    )
    reason: str = Field(
        description=(
            "invalid_geometry（几何标签缺失/无法解析/取值非法） | "
            "frame_of_reference_mismatch（FrameOfReferenceUID 不一致） | "
            "orientation_mismatch（ImageOrientationPatient 方向不一致） | "
            "duplicate_projection（法线投影位置重合） | "
            "ambiguous_output（同一匿名 SOP UID 对应多份输出） | "
            "unreadable（匿名输出不可读） | output_missing（磁盘文件缺失）"
        )
    )
    missing_or_invalid_tags: list[str] = Field(
        default=[], description="reason=invalid_geometry 时给出缺失的标签关键字"
    )
    frame_of_reference_uid: Optional[str] = Field(
        default=None, description="frame_of_reference_mismatch 时该组的 FrameOfReferenceUID"
    )
    expected_frame_of_reference_uid: Optional[str] = Field(
        default=None, description="frame_of_reference_mismatch 时的系列基准 UID"
    )
    image_orientation_patient: Optional[list[float]] = Field(
        default=None, description="orientation_mismatch 时该组实际方向余弦（6 分量）"
    )
    expected_image_orientation_patient: Optional[list[float]] = Field(
        default=None, description="orientation_mismatch 时的系列基准方向余弦"
    )
    normal: Optional[list[float]] = Field(
        default=None, description="orientation_mismatch 时该组叉积单位法线"
    )
    expected_normal: Optional[list[float]] = Field(
        default=None, description="orientation_mismatch 时的系列基准法线"
    )
    opposite_direction: Optional[bool] = Field(
        default=None,
        description=(
            "orientation_mismatch 时该组法线是否与系列基准法线反向"
            "（normal·expected_normal ≈ -1，即从相反一侧观察、投影排序方向相反）；"
            "绕法线旋转 180°（行/列同时反向、法线不变）时为 false"
        ),
    )
    projection: Optional[float] = Field(
        default=None, description="duplicate_projection 时的重合投影坐标（毫米）"
    )
    positions: list[dict] = Field(
        default=[],
        description="duplicate_projection 时给出各匿名 SOP UID 及其投影坐标",
    )
    message: str


class GeometryErrorDetail(BaseModel):
    code: str = Field(
        description="SERIES_NOT_FOUND（404） | JOB_NOT_READY（409） | "
        "SERIES_GEOMETRY_CONFLICT（409） | NO_SINGLE_FRAME_INSTANCES（409）"
    )
    message: str
    series_instance_uid: Optional[str] = None
    single_frame_instance_count: Optional[int] = Field(
        default=None, description="SERIES_GEOMETRY_CONFLICT 时已解析的单帧实例数"
    )
    conflict_count: Optional[int] = Field(
        default=None, description="SERIES_GEOMETRY_CONFLICT 时的冲突明细条数"
    )
    problems: list[GeometryProblemItem] = []


# --------------------------------------------------------------------------- #
# 系列 CT 管电压（KVP）逐帧分布（GET /jobs/{id}/series/{uid}/kvp）
# --------------------------------------------------------------------------- #
class KvpVoltageBucket(BaseModel):
    kvp: float = Field(description="管电压（kV）")
    frame_count: int = Field(description="该电压的帧数")


class KvpInstanceItem(BaseModel):
    sop_instance_uid: str = Field(description="匿名 SOPInstanceUID")
    output_file: Optional[str] = Field(
        default=None, description="任务 output 目录内的扁平文件名"
    )
    number_of_frames: int = Field(
        description="声明帧数（NumberOfFrames 缺省按 1；单帧文件按一帧处理）"
    )
    kvp_source: str = Field(
        description="per_frame（逐帧功能组） | shared（共享功能组） | "
        "top_level（顶层 KVP） | mixed（帧间来源不一致） | none（无任何 KVP）"
    )
    frame_kvps: list[Optional[float]] = Field(
        description="逐帧 KVP（kV），与帧顺序一致；null 表示该帧缺失/无法解析"
    )
    missing_kvp_frames: int = Field(description="本实例缺失 KVP 的帧数")


class SeriesKvpOut(BaseModel):
    series_instance_uid: str = Field(description="请求的匿名 SeriesInstanceUID")
    instance_count: int = Field(description="纳入统计的实例数")
    total_frames: int = Field(description="纳入统计的总帧数")
    missing_kvp_frames: int = Field(description="全系列缺失 KVP 的帧数")
    mixed_kvp: bool = Field(
        description="系列内是否混用多种管电压（distinct KVP > 1），"
        "用于发现同系列混用扫描参数"
    )
    voltage_distribution: list[KvpVoltageBucket] = Field(
        description="各管电压的帧数分布，按 kV 升序"
    )
    instances: list[KvpInstanceItem] = Field(
        description="逐实例明细，按匿名 SOPInstanceUID 字典序排列"
    )


class KvpProblemItem(BaseModel):
    sop_instance_uid: Optional[str] = Field(
        default=None, description="出问题实例的匿名 SOPInstanceUID（歧义时为该共同 UID）"
    )
    output_file: Optional[str] = Field(
        default=None, description="可定位时给出任务 output 目录内的扁平文件名"
    )
    output_files: list[str] = Field(
        default=[], description="ambiguous_output 时给出相互冲突的输出文件名"
    )
    reason: str = Field(
        description=(
            "frame_count_mismatch（NumberOfFrames 声明帧数与 "
            "PerFrameFunctionalGroupsSequence 条目数不一致） | "
            "invalid_number_of_frames（NumberOfFrames 取值非法） | "
            "ambiguous_output（同一匿名 SOP UID 对应多份输出） | "
            "unreadable（匿名输出不可读） | output_missing（磁盘文件缺失）"
        )
    )
    declared_number_of_frames: Optional[str] = Field(
        default=None,
        description="frame_count_mismatch / invalid_number_of_frames 时的声明帧数原文",
    )
    per_frame_item_count: Optional[int] = Field(
        default=None,
        description="frame_count_mismatch 时 PerFrameFunctionalGroupsSequence 的条目数",
    )
    message: str


class KvpErrorDetail(BaseModel):
    code: str = Field(
        description="SERIES_NOT_FOUND（404） | JOB_NOT_READY（409） | "
        "SERIES_KVP_CONFLICT（409）"
    )
    message: str
    series_instance_uid: Optional[str] = None
    conflict_count: Optional[int] = Field(
        default=None, description="SERIES_KVP_CONFLICT 时的冲突明细条数"
    )
    problems: list[KvpProblemItem] = []


# --------------------------------------------------------------------------- #
# CT 剂量 SR 照射事件解析（GET /jobs/{id}/instances/{uid}/dose-events）
# --------------------------------------------------------------------------- #
class DoseEventItem(BaseModel):
    index: int = Field(description="事件在 SR 内容顺序中的 0 基序号")
    ctdi_vol_mgy: float = Field(description="该事件的 CTDIvol（mGy）")
    dlp_mgy_cm: float = Field(description="该事件的 DLP（mGy·cm）")


class DoseEventsOut(BaseModel):
    sop_instance_uid: str = Field(description="请求的匿名 SOPInstanceUID")
    output_file: Optional[str] = Field(
        default=None, description="任务 output 目录内的扁平文件名"
    )
    event_count: int = Field(description="解析出的 CT 照射事件数")
    total_dlp_mgy_cm: float = Field(
        description="全部事件 DLP 之和（mGy·cm）；报告级累计值不参与合计"
    )
    events: list[DoseEventItem] = Field(
        description="按 SR 内容顺序排列的事件剂量（CTDIvol / DLP）"
    )


class DoseEventsProblemItem(BaseModel):
    sop_instance_uid: Optional[str] = Field(
        default=None, description="出问题实例的匿名 SOPInstanceUID"
    )
    output_file: Optional[str] = Field(
        default=None, description="任务 output 目录内的扁平文件名"
    )
    event_index: Optional[int] = Field(
        default=None, description="事件级问题对应的事件 0 基序号（文件级问题为 null）"
    )
    quantity: Optional[str] = Field(
        default=None, description="事件级问题涉及的量：ctdi_vol | dlp"
    )
    reason: str = Field(
        description=(
            "missing_value（事件内缺少该量） | "
            "duplicate_value（事件内该量出现多次） | "
            "invalid_value（数值缺失或无法解析为有限数值） | "
            "unit_mismatch（单位不符：CTDIvol 须为 mGy、DLP 须为 mGy.cm） | "
            "unreadable（匿名输出不可读） | "
            "not_dose_sr（不是 X-Ray Radiation Dose SR 或缺少 ContentSequence） | "
            "sop_instance_uid_mismatch（文件内 SOP UID 与请求不符）"
        )
    )
    units: Optional[str] = Field(
        default=None, description="unit_mismatch 时文件中的实际单位 CodeValue"
    )
    numeric_value: Optional[str] = Field(
        default=None, description="invalid_value 时 NumericValue 的原始文本"
    )
    message: str


class DoseEventsErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND（404） | INSTANCE_NOT_FOUND（404） | "
        "INSTANCE_OUTPUT_MISSING（404） | JOB_NOT_READY（409） | "
        "AMBIGUOUS_INSTANCE_OUTPUT（409） | DOSE_SR_CONFLICT（409）"
    )
    message: str
    sop_instance_uid: Optional[str] = None
    reason: Optional[str] = Field(
        default=None,
        description="INSTANCE_OUTPUT_MISSING 时为 output_missing；"
        "AMBIGUOUS_INSTANCE_OUTPUT 时为 ambiguous_output",
    )
    output_file: Optional[str] = Field(
        default=None, description="可定位时给出任务 output 目录内的扁平文件名"
    )
    output_files: list[str] = Field(
        default=[], description="reason=ambiguous_output 时给出相互冲突的输出文件名"
    )
    conflict_count: Optional[int] = Field(
        default=None, description="DOSE_SR_CONFLICT 时的问题明细条数"
    )
    problems: list[DoseEventsProblemItem] = []


# --------------------------------------------------------------------------- #
# SEG 分割覆盖率统计（GET /jobs/{id}/instances/{uid}/seg-coverage）
# --------------------------------------------------------------------------- #
class SegCoverageGroupItem(BaseModel):
    source_sop_instance_uid: str = Field(
        description="源图像的匿名 SOPInstanceUID（帧级 DerivationImageSequence/"
        "SourceImageSequence 引用，已与任务成功输出对齐）"
    )
    source_output_file: Optional[str] = Field(
        default=None, description="源图像在任务 output 目录内的扁平文件名"
    )
    segment_number: int = Field(description="段号（ReferencedSegmentNumber）")
    frame_count: int = Field(description="参与并集的 SEG 帧数")
    foreground_pixels: int = Field(description="并集掩码中的前景（1）像素数")
    image_pixels: int = Field(description="源图像像素数（Rows × Columns）")
    coverage: float = Field(
        description="覆盖率 = foreground_pixels / image_pixels（0..1）"
    )


class SegCoverageOut(BaseModel):
    sop_instance_uid: str = Field(description="请求的匿名 SEG SOPInstanceUID")
    output_file: Optional[str] = Field(
        default=None, description="SEG 在任务 output 目录内的扁平文件名"
    )
    segmentation_type: str = Field(
        description="固定为 BINARY（仅支持未压缩 BINARY 分割）"
    )
    rows: int = Field(description="SEG 掩码行数")
    columns: int = Field(description="SEG 掩码列数")
    number_of_frames: int = Field(description="SEG 声明帧数（NumberOfFrames）")
    group_count: int = Field(description="（源图像, 段号）分组数")
    groups: list[SegCoverageGroupItem] = Field(
        description="按（源 SOP UID, 段号）排序的覆盖率统计"
    )


class SegCoverageProblemItem(BaseModel):
    sop_instance_uid: Optional[str] = Field(
        default=None, description="出问题的匿名 SEG SOPInstanceUID"
    )
    output_file: Optional[str] = Field(
        default=None, description="SEG 在任务 output 目录内的扁平文件名"
    )
    frame_index: Optional[int] = Field(
        default=None, description="帧级问题对应的 0 基帧序号（文件级问题为 null）"
    )
    source_sop_instance_uid: Optional[str] = Field(
        default=None, description="源图像级问题对应的匿名源 SOPInstanceUID"
    )
    source_sop_instance_uids: list[str] = Field(
        default=[],
        description=(
            "multiple_source_references 时给出该帧引用的全部不同匿名源 "
            "SOPInstanceUID（去重、按字典序排列）"
        ),
    )
    source_output_file: Optional[str] = Field(
        default=None, description="源图像在任务 output 目录内的扁平文件名"
    )
    reason: str = Field(
        description=(
            "unreadable（SEG 输出不可读） | "
            "not_segmentation（不是 Segmentation Storage） | "
            "unsupported_segmentation_type（非 BINARY 分割） | "
            "unsupported_transfer_syntax（压缩/封装传输语法） | "
            "sop_instance_uid_mismatch（文件内 SOP UID 与请求不符） | "
            "invalid_pixel_metadata（非 1-bit 像素元数据） | "
            "invalid_number_of_frames（NumberOfFrames 缺失/非法） | "
            "frame_count_mismatch（逐帧功能组条目数与声明帧数不符） | "
            "missing_pixel_data（无 PixelData） | "
            "pixel_decode_failed（像素数据截断/无法解码） | "
            "missing_segment_number（帧缺少有效 ReferencedSegmentNumber） | "
            "missing_source_reference（帧缺少源图像引用） | "
            "multiple_source_references（同一帧在 DerivationImageSequence/"
            "SourceImageSequence 中引用两个及以上不同匿名 SOP UID，"
            "无法唯一确定源图像） | "
            "source_not_in_output（引用的源图像不在成功输出中） | "
            "source_ambiguous_output（源图像对应多份输出） | "
            "source_output_missing（源图像磁盘缺失） | "
            "source_unreadable（源图像不可读/缺少有效尺寸） | "
            "source_dimension_mismatch（源图像尺寸与掩码不一致）"
        )
    )
    output_files: list[str] = Field(
        default=[],
        description="source_ambiguous_output 时给出相互冲突的输出文件名",
    )
    declared_number_of_frames: Optional[str] = Field(
        default=None,
        description="invalid_number_of_frames / frame_count_mismatch 时的声明帧数原文",
    )
    per_frame_item_count: Optional[int] = Field(
        default=None,
        description="frame_count_mismatch 时 PerFrameFunctionalGroupsSequence 的条目数",
    )
    source_rows: Optional[int] = Field(
        default=None, description="source_dimension_mismatch 时源图像的行数"
    )
    source_columns: Optional[int] = Field(
        default=None, description="source_dimension_mismatch 时源图像的列数"
    )
    transfer_syntax_uid: Optional[str] = Field(
        default=None, description="unsupported_transfer_syntax 时的实际传输语法 UID"
    )
    segmentation_type: Optional[str] = Field(
        default=None, description="unsupported_segmentation_type 时的实际 SegmentationType"
    )
    message: str


class SegCoverageErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND（404） | INSTANCE_NOT_FOUND（404） | "
        "INSTANCE_OUTPUT_MISSING（404） | JOB_NOT_READY（409） | "
        "AMBIGUOUS_INSTANCE_OUTPUT（409） | SEG_COVERAGE_CONFLICT（409）"
    )
    message: str
    sop_instance_uid: Optional[str] = None
    reason: Optional[str] = Field(
        default=None,
        description="INSTANCE_OUTPUT_MISSING 时为 output_missing；"
        "AMBIGUOUS_INSTANCE_OUTPUT 时为 ambiguous_output",
    )
    output_file: Optional[str] = Field(
        default=None, description="可定位时给出任务 output 目录内的扁平文件名"
    )
    output_files: list[str] = Field(
        default=[], description="reason=ambiguous_output 时给出相互冲突的输出文件名"
    )
    conflict_count: Optional[int] = Field(
        default=None, description="SEG_COVERAGE_CONFLICT 时的问题明细条数"
    )
    problems: list[SegCoverageProblemItem] = []


# --------------------------------------------------------------------------- #
# 跨任务合并下载单个匿名检查（POST /jobs/merged-study/download）
# --------------------------------------------------------------------------- #
class MergedStudyDownloadRequest(BaseModel):
    job_ids: list[str] = Field(
        description="已完成（status=ready）任务 ID，2～5 个；不得重复",
        min_length=2,
        max_length=5,
    )
    study_instance_uid: str = Field(
        description=(
            "匿名后的 StudyInstanceUID（取自任一参与任务结果中的 "
            "studies[].new_study_instance_uid）"
        ),
        min_length=1,
    )

    @field_validator("job_ids")
    @classmethod
    def _strip_and_reject_duplicates(cls, value: list[str]) -> list[str]:
        ids = [str(jid).strip() for jid in value]
        if any(not jid for jid in ids):
            raise ValueError("任务 ID 不能为空字符串")
        if len(set(ids)) != len(ids):
            raise ValueError("任务 ID 不得重复")
        return ids

    @field_validator("study_instance_uid")
    @classmethod
    def _strip_study_uid(cls, value: str) -> str:
        uid = str(value).strip()
        if not uid:
            raise ValueError("study_instance_uid 不能为空字符串")
        return uid


class MergedStudyCopyItem(BaseModel):
    job_id: str = Field(description="该份物理输出所属任务 ID")
    output_file: str = Field(description="该任务 output 目录内的扁平匿名文件名")
    sha256: str = Field(description="该份输出文件内容的 SHA-256（小写十六进制）")


class MergedStudyProblemItem(BaseModel):
    sop_instance_uid: str = Field(description="出问题的匿名（已重写）SOPInstanceUID")
    reason: str = Field(
        description="missing_in_all_jobs（引用目标在所有指定任务中均不存在） | "
        "byte_conflict（同一匿名 SOP UID 的多份输出字节不一致）"
    )
    job_ids: list[str] = Field(
        default=[],
        description="byte_conflict 时给出相互冲突的输出来源任务 ID（缺失时为空）",
    )
    copies: list[MergedStudyCopyItem] = Field(
        default=[],
        description="byte_conflict 时给出各份冲突输出（任务/文件名/SHA-256）",
    )
    message: str


class MergedStudyErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND（404） | STUDY_NOT_FOUND（404） | "
        "JOBS_NOT_COMPLETED（409） | SECRET_FINGERPRINT_MISMATCH（409） | "
        "MERGED_REFERENCE_CLOSURE_INCOMPLETE（409） | "
        "ANONYMIZED_OUTPUT_UNREADABLE（500）"
    )
    message: str
    study_instance_uid: Optional[str] = None
    missing_job_ids: list[str] = []
    not_ready_jobs: list[dict[str, str]] = []
    fingerprints: dict[str, str] = {}
    problems: list[MergedStudyProblemItem] = []
    conflicting_sop_instance_uids: list[str] = Field(
        default=[],
        description="同一匿名 SOP UID 对应字节不一致输出的冲突 UID 列表",
    )
    missing_sop_instance_uids: list[str] = Field(
        default=[],
        description="引用目标在所有指定任务成功输出中均不存在的匿名 UID 列表",
    )
