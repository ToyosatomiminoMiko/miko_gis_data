#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# (copyright) Kawaii Femboy Technology Co., Ltd.
"""miko-gis-data 技能的唯一下载脚本: 检查 / 下载 / 发布地图原始数据的 manifest.

是什么: 把外部数据源(Natural Earth / NOAA ETOPO 2022 / GADM / OSM Overpass /
HydroBASINS / OCHA)的原始文件下载或解压进数据根, 逐项核对就位与版本, 并发布
``<数据根>/manifest.json`` 给渲染仓 Cartopy_file 读取. 本脚本只保管"下载原样"的原始
数据, 不产生派生 GeoJSON: 裁剪, 补缝, 多边形化都在渲染仓做.

为什么是一个文件: 它本来只有 check / download / publish 三个命令, 拆成包 + 文档 +
自检脚本只会增加维护面; 数据源的 URL, 许可, 署名与区域清单全部集中在下面的 SOURCES /
REGIONS 两个常量里, 改数据源只改一处.

用法::

    python3 ~/.dsh/skills/miko-gis-data/gis_data.py [--data-root PATH] check
    python3 ~/.dsh/skills/miko-gis-data/gis_data.py [--data-root PATH] download [--group ...]
    python3 ~/.dsh/skills/miko-gis-data/gis_data.py [--data-root PATH] publish [--check]
    python3 ~/.dsh/skills/miko-gis-data/gis_data.py [--data-root PATH] all

数据根默认是 ``~/miko_gis_data/data``, 可用 ``--data-root`` 或环境变量
``MIKO_GIS_DATA_ROOT`` 覆盖; 渲染仓根 ``config.toml`` 的 ``data_root`` 必须指向同一处.
坑与约定(ETOPO 经度 0~360, 续传探测, 版本文件, Overpass 完整性标记等)见技能
``SKILL.md``. 本脚本依赖系统 ``wget`` 与 ``unzip``, Python 侧只有标准库.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# ============================
# 路径常量
# ============================
# 数据根默认值: 用户目录下的固定位置, 与仓库路径无关; 换位置用 --data-root 或
# 环境变量 MIKO_GIS_DATA_ROOT, 不必改代码.
DEFAULT_DATA_ROOT = Path.home() / "miko_gis_data" / "data"
DATA_ROOT_ENV = "MIKO_GIS_DATA_ROOT"
# 提示里给出的命令前缀: 与技能安装位置一致(技能目录 ~/.dsh/skills/miko-gis-data/).
SKILL_SCRIPT = "python3 ~/.dsh/skills/miko-gis-data/gis_data.py"

# 运行时数据根; main() 按 --data-root / 环境变量覆盖, 其余函数只读它.
ROOT: Path = DEFAULT_DATA_ROOT


def data_root() -> Path:
    """返回数据根(自动建目录); manifest, cartopy/ 缓存与各区域数据都在这里."""
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


def natural_earth_dir(category: str) -> Path:
    """共享 Natural Earth 缓存目录, 布局与 cartopy 的 data_dir 一致."""
    return data_root() / "cartopy" / "shapefiles" / "natural_earth" / category


def natural_earth_path(resolution: str, category: str, name: str) -> Path:
    """某个 Natural Earth 图层的 ``.shp`` 路径(不联网); 整套 shapefile 一起出现."""
    return natural_earth_dir(category) / f"ne_{resolution}_{name}.shp"


# ============================
# 数据源登记
# ============================
# 下载时的 User-Agent: 个别站点(S3/ERDDAP)对脚本默认 UA 限速或拒绝, 统一带浏览器 UA.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Overpass 公共实例: 依次尝试, 前一个 502/超时就换下一个.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


@dataclass(frozen=True)
class Source:
    """一个外部数据源: 版本口径, URL 模板, 发布页, 许可与进图署名.

    version 是版本口径, 没有版本概念的数据源(Overpass)留空串; homepage 写长期有效的
    发布页(不写会随参数变化的下载地址); credit 是进图/产物必须出现的署名文字.
    """

    id: str
    title: str
    version: str
    url: str
    homepage: str
    license: str
    credit: str


SOURCES: dict[str, Source] = {
    source.id: source
    for source in (
        Source(
            id="natural-earth",
            title="Natural Earth",
            # S3 地址不带版本: 换版会原地覆盖同名文件, 版本只能靠缓存里的
            # *.VERSION.txt 钉住(见 SKILL.md 的"版本口径").
            version="5.1.1",
            url="https://naturalearth.s3.amazonaws.com/{resolution}_{category}/{name}.zip",
            homepage="https://www.naturalearthdata.com",
            license="public domain",
            credit="Natural Earth 5.1.1",
        ),
        Source(
            id="etopo-2022",
            title="NOAA ETOPO 2022",
            version="v1 (15 arc-second)",
            url="https://oceanwatch.pifsc.noaa.gov/erddap/griddap/ETOPO_2022_v1_15s.nc",
            homepage="https://www.ncei.noaa.gov/products/etopo-global-relief-model",
            license="public domain (US Government work)",
            credit="NOAA ETOPO 2022 (15″)",
        ),
        Source(
            id="gadm-4.1",
            title="GADM",
            version="4.1",
            url="https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_{iso}_1.json",
            homepage="https://gadm.org",
            license="free for academic and non-commercial use",
            credit="GADM 4.1",
        ),
        Source(
            id="hydrobasins",
            title="HydroBASINS (HydroSHEDS)",
            version="v1c",
            url="https://data.hydrosheds.org/file/HydroBASINS/standard/{archive}",
            homepage="https://www.hydrosheds.org/products/hydrobasins",
            license="free, attribution required",
            credit="HydroBASINS (HydroSHEDS) v1c",
        ),
        Source(
            id="ocha-hno-2021",
            title="OCHA 2021 HNO GCA/NGCA",
            version="2021",
            url="https://data.humdata.org/dataset/0a24205d-084c-4ef9-9d53-9349bacbde1e/",
            homepage="https://data.humdata.org/dataset/0a24205d-084c-4ef9-9d53-9349bacbde1e/",
            license="per HDX dataset page",
            credit="OCHA 2021 HNO",
        ),
        Source(
            id="openstreetmap",
            title="OpenStreetMap",
            version="",
            url=OVERPASS_ENDPOINTS[0],
            homepage="https://www.openstreetmap.org/copyright",
            license="ODbL 1.0",
            # ODbL 要求署名文字, 不能省略.
            credit="(C) OpenStreetMap contributors (ODbL)",
        ),
    )
}


def source(source_id: str) -> Source:
    """按 id 取数据源; 写错时立刻报错并列出已登记 id."""
    try:
        return SOURCES[source_id]
    except KeyError:
        raise KeyError(f"未登记的数据源 {source_id!r}; 已登记: {sorted(SOURCES)}") from None


def ne_url(resolution: str, category: str, name: str) -> str:
    """Natural Earth 单个图层的下载地址(与 cartopy 的 NEShpDownloader 同一口径)."""
    return source("natural-earth").url.format(
        resolution=resolution, category=category, name=name
    )


def erddap_url(
    source_id: str,
    *,
    stride: int,
    lon_range: tuple[float, float],
    lat_range: tuple[float, float],
) -> str:
    """NOAA ERDDAP 动态子集地址: 查询里是坐标值, stride 是索引步长(2 即 30 弧秒).

    ERDDAP 的经度是 0~360°: 跨 180° 的区域(如苏联)调用方要先把负经度 +360 再传进来.
    """
    base = source(source_id).url
    min_lon, max_lon = lon_range
    min_lat, max_lat = lat_range
    query = (
        f"z%5B({min_lat}):{stride}:({max_lat})%5D"
        f"%5B({min_lon}):{stride}:({max_lon})%5D"
    )
    return f"{base}?{query}"


def gadm_url(iso: str) -> str:
    """GADM 4.1 一级行政区 JSON 地址(iso 用三字母代码, 如 RUS)."""
    return source("gadm-4.1").url.format(iso=iso)


def hydrobasins_url(archive: str) -> str:
    """HydroBASINS 压缩包地址(archive 形如 hybas_eu_lev05_v1c.zip)."""
    return source("hydrobasins").url.format(archive=archive)


def overpass_endpoints() -> tuple[str, ...]:
    """Overpass 查询接口; 按顺序轮换, 全部失败才算失败."""
    return OVERPASS_ENDPOINTS


# ============================
# 数据集与检查
# ============================
@dataclass(frozen=True)
class BBox:
    """数据覆盖范围: 经度在前, 纬度在后, 每个方向 (min, max); 与渲染仓 Extent 同序."""

    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float

    def __post_init__(self) -> None:
        values = {
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
        }
        for name, raw in values.items():
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"BBox.{name} 必须是有限数, 得到 {raw!r}")
            object.__setattr__(self, name, number)
        if not self.lon_min < self.lon_max:
            raise ValueError(f"BBox 需要 lon_min < lon_max, 得到 {self}")
        if not self.lat_min < self.lat_max:
            raise ValueError(f"BBox 需要 lat_min < lat_max, 得到 {self}")
        if not (-180.0 <= self.lon_min and self.lon_max <= 360.0):
            raise ValueError(f"BBox 经度超出 [-180, 360]: {self}")
        if not (-90.0 <= self.lat_min and self.lat_max <= 90.0):
            raise ValueError(f"BBox 纬度超出 [-90, 90]: {self}")

    def as_dict(self) -> dict[str, float]:
        """manifest 里的写法; 键名显式, 读的人不用猜顺序."""
        return {
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
        }


@dataclass(frozen=True)
class Dataset:
    """一份原始数据: 来源/版本/许可 + 本地位置 + 校验方式.

    kind:
        ``file``        单个文件, 由 fetch 下载(dest 是文件);
        ``zip``         下载压缩包并解压到 dest 目录;
        ``cartopy-ne``  Natural Earth 图层, 放在共享缓存 ``<数据根>/cartopy/``;
        ``script``      下载脚本自行抓取(Overpass 原始响应), 只做存在性检查.
    bbox 是数据本身覆盖的经纬度范围, 渲染仓出图时据此断言"下载范围盖住画框".
    version 覆盖 Source.version, 只给需要单独钉版本的数据集用.
    version_file 是相对区域数据根的 ``*.VERSION.txt``, 内容第一行即版本号.
    files 是判定"已就位"的 glob(相对区域数据根), 缺省用 dest.
    optional 表示可选缓存: 缺了不影响出图, 检查时不记为失败.
    """

    id: str
    kind: str = "file"
    source_id: str | None = None
    url: str = ""
    dest: str = ""
    bbox: BBox | None = None
    stride: int | None = None
    version: str | None = None
    version_file: str = ""
    files: tuple[str, ...] = ()
    resolution: str = ""
    category: str = ""
    name: str = ""
    optional: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Dataset.id 不能为空")
        allowed = {"file", "zip", "cartopy-ne", "script", "local"}
        if self.kind not in allowed:
            raise ValueError(f"Dataset.kind 必须是 {sorted(allowed)}, 得到 {self.kind!r}")
        if self.kind in ("file", "zip") and not (self.url and self.dest):
            raise ValueError(f"Dataset({self.id}) 的 kind={self.kind} 需要 url 和 dest")
        if self.kind == "cartopy-ne" and not (
            self.resolution and self.category and self.name
        ):
            raise ValueError(
                f"Dataset({self.id}) 的 kind=cartopy-ne 需要 resolution/category/name"
            )

    @property
    def source(self) -> Source | None:
        """登记在 SOURCES 里的数据源; 项目自带数据没有."""
        return None if self.source_id is None else source(self.source_id)

    @property
    def version_label(self) -> str:
        """实际版本标签: 数据集覆盖值优先, 否则用数据源的版本."""
        if self.version:
            return self.version
        parent = self.source
        return "" if parent is None else parent.version


@dataclass(frozen=True)
class CheckLine:
    """一条数据检查结果, 供 check 命令打印."""

    dataset_id: str
    group: str
    label: str
    state: str  # ok | missing | drifted | optional
    detail: str

    @property
    def failed(self) -> bool:
        """missing 与 drifted 都算失败: 版本漂移会让渲染产物与文档不一致."""
        return self.state in ("missing", "drifted")


@dataclass
class DatasetSet:
    """一个区域的数据集集合: "这个区域要哪些原始数据"的唯一真源."""

    group: str
    root: Path
    items: tuple[Dataset, ...]

    def __post_init__(self) -> None:
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Dataset.id 重复: {ids}")
        if not self.group:
            raise ValueError("DatasetSet.group 不能为空(用于检查分组显示)")

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def get(self, dataset_id: str) -> Dataset:
        """按 id 取数据集; 写错时列出可用 id."""
        for item in self.items:
            if item.id == dataset_id:
                return item
        raise KeyError(
            f"{self.group}: 未声明数据集 {dataset_id!r}; 已声明: {[i.id for i in self.items]}"
        )

    @property
    def raw_dir(self) -> Path:
        """下载原样(含解压目录)的存放目录; 本技能唯一的数据目录."""
        return self.root / "raw"

    def ne_path(self, dataset: Dataset | str) -> Path:
        """Natural Earth 图层在共享缓存里的 ``.shp`` 路径(不联网)."""
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        return natural_earth_path(item.resolution, item.category, item.name)

    def path(self, dataset: Dataset | str) -> Path:
        """数据集在本地的主路径(下载目标 / 解压目录)."""
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        if item.kind == "cartopy-ne":
            return self.ne_path(item)
        return self.root / item.dest

    def patterns(self, dataset: Dataset | str) -> tuple[str, ...]:
        """判定"已就位"用的 glob(相对区域数据根)."""
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        return item.files or (item.dest,)

    def exists(self, dataset: Dataset | str) -> bool:
        """数据是否已就位; 不联网, 只查本地.

        注意 ``Path.glob`` 返回生成器, 生成器对象本身恒为真: 必须真正取出一个匹配项
        再判断, 否则检查会对任何数据集都报"已存在".
        """
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        if item.kind == "cartopy-ne":
            return self.path(item).is_file()
        return any(
            next(iter(self.root.glob(pattern)), None) is not None
            for pattern in self.patterns(item)
        )

    def existing_path(self, dataset: Dataset | str) -> Path:
        """实际命中的已就绪路径; 打印详情与判定就位用同一口径.

        为什么不能用 dest: ``files`` 是 glob, 同一数据集可能命中变体文件(如不同 ETOPO
        步长的 ``*_stride4.nc``), 打印 dest 会报出一个磁盘上不存在的路径.
        """
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        if item.kind == "cartopy-ne":
            return self.ne_path(item)
        for pattern in self.patterns(item):
            match = next(iter(self.root.glob(pattern)), None)
            if match is not None:
                return match
        return self.path(item)

    def version_of(self, dataset: Dataset | str) -> str:
        """读版本文件第一行; 没有版本文件时返回空串.

        ``cartopy-ne`` 的版本证据在共享缓存里, 其余在区域数据目录.
        """
        item = self.get(dataset) if isinstance(dataset, str) else dataset
        if item.kind == "cartopy-ne":
            version_file = natural_earth_dir(item.category) / (
                f"ne_{item.resolution}_{item.name}.VERSION.txt"
            )
        else:
            if not item.version_file:
                return ""
            version_file = self.root / item.version_file
        try:
            text = version_file.read_text(encoding="utf-8", errors="replace")
            return text.splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def check(self) -> list[CheckLine]:
        """逐条检查本区域的数据; 版本不符也算失败(换版会改变渲染产物)."""
        lines: list[CheckLine] = []
        for item in self.items:
            if item.kind == "cartopy-ne":
                target = self.ne_path(item)
                if not target.is_file():
                    lines.append(
                        CheckLine(
                            item.id, self.group, item.note or item.name, "missing", str(target)
                        )
                    )
                    continue
            if not self.exists(item):
                lines.append(
                    CheckLine(
                        dataset_id=item.id,
                        group=self.group,
                        label=item.note or item.dest or item.name,
                        state="optional" if item.optional else "missing",
                        detail=", ".join(self.patterns(item)),
                    )
                )
                continue
            found = self.version_of(item)
            expected = item.version_label
            if found and expected and found != expected:
                lines.append(
                    CheckLine(
                        dataset_id=item.id,
                        group=self.group,
                        label=item.note or item.dest or item.name,
                        state="drifted",
                        detail=f"本地版本 {found} != 期望 {expected}",
                    )
                )
                continue
            lines.append(
                CheckLine(
                    dataset_id=item.id,
                    group=self.group,
                    label=item.note or item.dest or item.name,
                    state="ok",
                    detail=str(self.existing_path(item)),
                )
            )
        return lines


def dataset_set(group: str, root: Path, items) -> DatasetSet:
    """构造一个区域的数据集集合(写起来比直接 new 短)."""
    return DatasetSet(group=group, root=Path(root), items=tuple(items))


def ne_layers(*layers: tuple[str, str, str]) -> list[Dataset]:
    """把 Natural Earth 图层写成一行一个: ``(id, "<resolution>/<category>/<name>", note)``.

    逐条展开成七八个字段只会让人数不清"这个区域用了哪些底图"; id 必须显式写, 因为
    渲染仓的 sheet 配置按 id 引用数据集, id 不一定能从图层名推出来.
    """
    items: list[Dataset] = []
    for dataset_id, path, note in layers:
        parts = path.split("/")
        if len(parts) != 3:
            raise ValueError(
                f"Natural Earth 图层路径须为 <resolution>/<category>/<name>: {path!r}"
            )
        resolution, category, name = parts
        items.append(
            Dataset(
                id=dataset_id,
                kind="cartopy-ne",
                source_id="natural-earth",
                resolution=resolution,
                category=category,
                name=name,
                note=note,
            )
        )
    return items


# ============================
# 下载层: wget / unzip 薄封装
# ============================
# 内容魔数: 服务器返回错误页时 HTTP 状态码仍可能是 200, 不看文件头就会把 HTML 当数据
# 缓存下来, 到出图时才报难懂的错. 后缀是"这个文件是什么格式"的唯一线索, 按落点后缀选.
MAGIC_NETCDF = (b"CDF", b"\x89HDF")
MAGIC_ZIP = (b"PK",)
MAGIC_JSON = (b"{", b"[")
_MAGIC_BY_SUFFIX = {".nc": MAGIC_NETCDF, ".json": MAGIC_JSON, ".zip": MAGIC_ZIP}

# 统一下载参数: 失败重试 3 次, 退避等待, 单次网络操作超时 5 分钟.
# --no-hsts: wget 2.x 默认把 HSTS 缓存写进 HOME; HOME 只读的机器上每次都会警告.
_WGET_OPTIONS = (
    "--tries=3",
    "--waitretry=5",
    "--timeout=300",
    "--retry-connrefused",
    "--no-hsts",
    f"--user-agent={USER_AGENT}",
)

# wget 1.x 把 --spider -S 的头打到 stderr, 2.x 打到 stdout 且 HTTP/2 用 ":status:".
_STATUS_206 = re.compile(r"(?::status:\s*|HTTP response |HTTP/\S+ )206\b")


def _tool(name: str) -> str:
    """返回外部命令的绝对路径; 缺命令时给出可执行的提示."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"缺少系统命令 {name}: 下载/解压交给它做, 请先安装 wget 与 unzip"
        )
    return path


def _wget(*args: str) -> list[str]:
    """拼一条 wget 命令, 统一带上重试与 User-Agent 等公共参数."""
    return [_tool("wget"), *_WGET_OPTIONS, *args]


def _run(argv: list[str], what: str) -> None:
    """跑一条外部命令; 退出码非 0 时抛错(命令自己已把细节打到终端)."""
    result = subprocess.run(argv)
    if result.returncode != 0:
        raise RuntimeError(f"{what} 失败(退出码 {result.returncode})")


def _supports_resume(url: str) -> bool:
    """探测服务器对 Range 请求是否真的返回 206; 不确定时按"不支持"处理.

    为什么必须探测: wget 2.x 对不支持 Range 的服务器(ERDDAP 动态子集就是)会返回 200
    却把完整响应接在旧 ``.part`` 后面(退出码仍是 0), 拼出坏文件. 用 HEAD 只要状态码,
    不花流量; 探测失败也按不支持处理, 重下一个文件比拼出坏文件便宜.
    """
    try:
        result = subprocess.run(
            _wget(
                "--spider",
                "--server-response",
                "--header=Range: bytes=0-0",
                "--tries=1",
                "--timeout=20",
                "-O",
                os.devnull,
                url,
            ),
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and _STATUS_206.search(output) is not None


def _check_magic(path: Path, magic: tuple[bytes, ...], url: str) -> None:
    """校验文件头; 不符时删掉半成品并报错(否则下次会当作已下载)."""
    with open(path, "rb") as handle:
        head = handle.read(8)
    if magic and not any(head.startswith(prefix) for prefix in magic):
        path.unlink(missing_ok=True)
        preview = head.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"下载内容不是预期的二进制格式(可能是错误页): {url} 头部={preview!r}"
        )


def fetch_file(url: str, dest: Path, *, force: bool = False) -> Path:
    """把 url 下载到 dest; 已存在且非空时跳过, 中断过则尽量续传.

    先写 ``.part`` 再改名: 中途失败时目标文件不存在, 下次不会把半截文件当成已完成.
    续传由 ``_supports_resume`` 决定: 服务器不认 Range 时删掉 ``.part`` 从零下载.
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"已存在, 跳过: {dest}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if force:
        part.unlink(missing_ok=True)
    elif part.exists():
        if _supports_resume(url):
            print(f"续传未完成的下载: {part}")
        else:
            print(f"服务器不支持断点续传, 从零下载: {dest.name}")
            part.unlink()

    print(f"下载 {dest.name}: {url}")
    _run(_wget("-c", "-O", str(part), url), f"下载 {dest.name}")
    _check_magic(part, _MAGIC_BY_SUFFIX.get(dest.suffix, ()), url)
    part.replace(dest)
    print(f"完成: {dest} ({dest.stat().st_size / 1048576:.1f} MiB)")
    return dest


def post(url: str, body: bytes, dest: Path, *, force: bool = False) -> Path:
    """POST 一段请求体并缓存响应(Overpass 这类实时查询接口用).

    已缓存过就不再请求(公共实例有限流); POST 没有断点续传, 残留 ``.part`` 一律丢弃.
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"已存在, 跳过: {dest}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    print(f"请求 {dest.name}: {url}")
    try:
        _run(
            _wget("--post-data", body.decode("ascii"), "-O", str(part), url),
            f"请求 {dest.name}",
        )
        if not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError(f"{dest.name} 响应为空: {url}")
        part.replace(dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    print(f"完成: {dest} ({dest.stat().st_size / 1024:.1f} KiB)")
    return dest


def extract_zip(archive: Path, dest_dir: Path, *, force: bool = False) -> Path:
    """把压缩包整体解压到 dest_dir; 已解压过就跳过(用 ``.unpacked`` 标记)."""
    archive = Path(archive)
    dest_dir = Path(dest_dir)
    marker = dest_dir / ".unpacked"
    if marker.is_file() and not force:
        print(f"已解压, 跳过: {dest_dir}")
        return dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [_tool("unzip"), "-o", "-q", str(archive), "-d", str(dest_dir)],
        f"解压 {archive.name}",
    )
    marker.write_text(archive.name + "\n", encoding="utf-8")
    print(f"已解压: {dest_dir}")
    return dest_dir


def extract_members(archive: Path, dest_dir: Path, pattern: str) -> None:
    """只解压 zip 里文件名匹配 pattern 的成员(共享 Natural Earth 缓存用)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [_tool("unzip"), "-o", "-q", str(archive), pattern, "-d", str(dest_dir)],
        f"解压 {archive.name} 的 {pattern}",
    )


# ============================
# 共享 Natural Earth 图层
# ============================
def shared_layers(catalogs) -> list[Dataset]:
    """汇总所有区域用到的 Natural Earth 图层(按 分辨率/类别/名称 去重).

    同一图层会被多个区域声明, 共享缓存只该下载一次; 保留第一次出现的 Dataset.
    """
    seen: dict[tuple[str, str, str], Dataset] = {}
    for catalog in catalogs:
        for dataset in catalog:
            if dataset.kind != "cartopy-ne":
                continue
            key = (dataset.resolution, dataset.category, dataset.name)
            seen.setdefault(key, dataset)
    return list(seen.values())


def fetch_shared_layers(catalogs) -> int:
    """下载缺失的共用 Natural Earth 图层; 返回失败数量(0 = 全部就位).

    已存在的一律跳过, 共享缓存没有 --force: 换版本属于要单独评审的改动, 要覆盖时
    先手动删掉目标 shapefile.
    """
    layers = shared_layers(catalogs)
    print(f"共用 Natural Earth 图层: {len(layers)} 个(来自 {len(catalogs)} 个区域)")
    failures = 0
    for dataset in layers:
        target = natural_earth_path(dataset.resolution, dataset.category, dataset.name)
        label = f"ne_{dataset.resolution}_{dataset.name}"
        if target.is_file():
            print(f"跳过(项目缓存已存在): {label}")
            continue
        url = ne_url(dataset.resolution, dataset.category, dataset.name)
        print(f"下载: {label}")
        with tempfile.TemporaryDirectory(prefix="miko-gis-ne-") as tmp:
            archive = Path(tmp) / f"{label}.zip"
            try:
                fetch_file(url, archive)
                extract_members(archive, target.parent, f"{target.stem}.*")
            except Exception as exc:  # noqa: BLE001 - 单层失败不中断其余图层
                failures += 1
                print(f"  失败: {exc}")
                continue
        if not target.is_file():
            failures += 1
            print(f"  失败: 解压后仍缺 {target}")
            continue
        print(f"  完成: {target}")
    return failures


# ============================
# 区域登记: 每个区域一个构造函数
# ============================
def _america_army(root: Path) -> DatasetSet:
    """美军基地演示图: 没有自有数据, 只用共享的 110m 底图."""
    layers = ne_layers(
        ("ne-110m-land", "110m/physical/land", "Natural Earth 110m 陆地(演示底图)"),
        ("ne-110m-ocean", "110m/physical/ocean", "Natural Earth 110m 海洋(演示底图)"),
        (
            "ne-110m-coastline",
            "110m/physical/coastline",
            "Natural Earth 110m 海岸线(演示底图)",
        ),
        (
            "ne-110m-borders",
            "110m/cultural/admin_0_boundary_lines_land",
            "Natural Earth 110m 国界(演示底图)",
        ),
        ("ne-110m-lakes", "110m/physical/lakes", "Natural Earth 110m 湖泊(演示底图)"),
        (
            "ne-110m-rivers",
            "110m/physical/rivers_lake_centerlines",
            "Natural Earth 110m 河流(演示底图)",
        ),
    )
    return dataset_set("美军基地", root, layers)


# ---- 缅甸: 两套 ETOPO + 掸邦 Overpass 分组 + 行政区原始数据 ----
# ETOPO 下载范围由画框外扩: 圆锥投影的矩形画框顶边会张开, 纬度方向也要留余量.
MYANMAR_ETOPO_MYANMAR = BBox(85.3, 107.7, 4.9, 31.1)
# 掸邦放大图: 画框比国土小, 下载范围直接等于画框.
MYANMAR_ETOPO_SHAN = BBox(94.6, 102.1, 18.8, 25.2)
# Overpass relation 分组: 一次请求多个 relation 时响应体积随成员 way 数增长, 分组避免超时.
# 佤邦(9399863)单独一组: 与 5 个县合在一起请求时响应过大, 公共实例容易 504.
MYANMAR_GROUPS: tuple[tuple[int, ...], ...] = (
    (16991276, 17632959, 17020740, 17020743, 17020742, 17020741),
    (9399863,),
    (14039305, 14007121, 14039309, 14035206, 14006889),
    (16742701, 16742698, 16742699, 16742700),
    (5996485, 5996480),
    (5996477,),
    (17883478,),
)
# 国界只取画框附近一段: 国家关系全量几何有几十 MB, 用 out geom(bbox) 只返回 bbox 内成员.
# 顺序是 (南, 西, 北, 东); 比画框外扩 0.5~1.3°, 覆盖投影后画框四角的经纬度外扩.
MYANMAR_BORDER_RELATIONS: tuple[int, ...] = (50371, 270056, 49903, 2067731)
MYANMAR_BORDER_BBOX = (18.3, 94.0, 25.6, 102.5)
# 内比都联邦区 relation; 走 Overpass 缓存.
NAYPYIDAW_RELATION_ID = 5996482


def _myanmar(root: Path) -> DatasetSet:
    """缅甸: 两套 ETOPO 子集, 共享底图, GADM/OSM 原始响应, 河流 5.0.0 副本."""
    etopo_myanmar = Dataset(
        id="etopo-myanmar",
        kind="file",
        source_id="etopo-2022",
        url=erddap_url(
            "etopo-2022",
            stride=2,
            lon_range=(MYANMAR_ETOPO_MYANMAR.lon_min, MYANMAR_ETOPO_MYANMAR.lon_max),
            lat_range=(MYANMAR_ETOPO_MYANMAR.lat_min, MYANMAR_ETOPO_MYANMAR.lat_max),
        ),
        dest="raw/ETOPO_2022_v1_15s_myanmar_stride2.nc",
        files=("raw/ETOPO_2022_v1_15s_myanmar_stride*.nc",),
        bbox=MYANMAR_ETOPO_MYANMAR,
        stride=2,
        note="ETOPO 2022 缅甸区域高程(stride 2 = 30 弧秒)",
    )
    etopo_shan = Dataset(
        id="etopo-northern-shan",
        kind="file",
        source_id="etopo-2022",
        url=erddap_url(
            "etopo-2022",
            stride=1,
            lon_range=(MYANMAR_ETOPO_SHAN.lon_min, MYANMAR_ETOPO_SHAN.lon_max),
            lat_range=(MYANMAR_ETOPO_SHAN.lat_min, MYANMAR_ETOPO_SHAN.lat_max),
        ),
        dest="raw/ETOPO_2022_v1_15s_northern_shan_stride1.nc",
        files=("raw/ETOPO_2022_v1_15s_northern_shan_stride*.nc",),
        bbox=MYANMAR_ETOPO_SHAN,
        stride=1,
        note="ETOPO 2022 掸邦区域高程(15 弧秒)",
    )
    layers = ne_layers(
        (
            "ne-countries-10m",
            "10m/cultural/admin_0_countries",
            "Natural Earth 10m 国家多边形(海陆底色与国界轮廓)",
        ),
        (
            "ne-coastline-10m",
            "10m/physical/coastline",
            "Natural Earth 10m 海岸线(国界剪掉沿海段的蒙版)",
        ),
        (
            "ne-borders-10m",
            "10m/cultural/admin_0_boundary_lines_land",
            "Natural Earth 10m 陆上国界(邻国参照线)",
        ),
    )
    # 这份河流是 5.0.0 副本(图面河网用它): 换成 5.1.1 会改变河网几何, 所以钉住版本
    # 而不跟共享缓存走.
    rivers = Dataset(
        id="ne-rivers-10m",
        kind="zip",
        source_id="natural-earth",
        version="5.0.0",
        url=ne_url("10m", "physical", "rivers_lake_centerlines"),
        dest="raw/ne_10m_rivers_lake_centerlines",
        version_file=(
            "raw/ne_10m_rivers_lake_centerlines/"
            "ne_10m_rivers_lake_centerlines.VERSION.txt"
        ),
        files=(
            "raw/ne_10m_rivers_lake_centerlines/ne_10m_rivers_lake_centerlines.shp",
        ),
        note="Natural Earth 10m 河流(缅甸行政区图用, 版本 5.0.0 副本)",
    )
    # 就位判定只认 .complete: 下载是逐组的, 只看 group_*.json 会把半套数据报成已存在.
    shan = Dataset(
        id="osm-shan-regions",
        kind="script",
        source_id="openstreetmap",
        dest="raw/regions/osm_shan_relations",
        files=("raw/regions/osm_shan_relations/.complete",),
        note=(
            "掸邦各特区/民地武控制区 relation 的 Overpass 原始响应"
            "(每组一个 group_XX.json; 全部成功才写 .complete)"
        ),
    )
    borders = Dataset(
        id="osm-country-borders",
        kind="script",
        source_id="openstreetmap",
        dest="raw/regions/osm_country_borders.json",
        note="画框附近国界 relation 的 Overpass 原始响应(bbox 裁剪, 单文件)",
    )
    gadm = Dataset(
        id="gadm41-mmr",
        kind="script",
        source_id="gadm-4.1",
        dest="raw/gadm41_MMR/gadm41_MMR_1.json",
        optional=True,
        note="GADM 4.1 缅甸一级行政区原始 JSON(行政区图原始数据)",
    )
    naypyidaw = Dataset(
        id="osm-naypyidaw",
        kind="script",
        source_id="openstreetmap",
        dest="raw/osm_naypyidaw_union_territory.json",
        optional=True,
        note="内比都联邦区 OSM 缓存",
    )
    return dataset_set(
        "缅甸",
        root,
        [etopo_myanmar, etopo_shan, *layers, rivers, shan, borders, gadm, naypyidaw],
    )


# ---- 苏联: 只有一套 ETOPO 子集(stride 8 = 2 弧分) ----
# 经度 15~195°E(跨 180° 用 195 而不是 -165, ERDDAP 的经度是 0~360°), 纬度 20~90°N.
# 纬度不从 15°N 起: 磁盘上已有的子集只从 20°N 起, 画框底边靠底图留白.
USSR_ETOPO_BBOX = BBox(15.0, 195.0, 20.0, 90.0)


def _ussr(root: Path) -> DatasetSet:
    """苏联: ETOPO 子集 + 10m 国家多边形 + 110m 海岸线(共享缓存)."""
    etopo = Dataset(
        id="etopo-ussr",
        kind="file",
        source_id="etopo-2022",
        url=erddap_url(
            "etopo-2022",
            stride=8,
            lon_range=(USSR_ETOPO_BBOX.lon_min, USSR_ETOPO_BBOX.lon_max),
            lat_range=(USSR_ETOPO_BBOX.lat_min, USSR_ETOPO_BBOX.lat_max),
        ),
        dest="raw/ETOPO_2022_v1_15s_ussr_stride8.nc",
        files=("raw/ETOPO_2022_v1_15s_ussr_stride*.nc",),
        bbox=USSR_ETOPO_BBOX,
        stride=8,
        note="ETOPO 2022 苏联区域高程(stride 8 = 2 弧分)",
    )
    layers = ne_layers(
        (
            "ne-countries-10m",
            "10m/cultural/admin_0_countries",
            "Natural Earth 10m 国家多边形(苏联轮廓)",
        ),
        (
            "ne-coastline-110m",
            "110m/physical/coastline",
            "Natural Earth 110m 海岸线(苏联图海岸线, 黑色细线)",
        ),
    )
    return dataset_set("苏联", root, [etopo, *layers])


# ---- 乌克兰: ETOPO/GADM/OCHA + 6 份 OSM 原始响应 ----
# 画框: 国土约 22.1~40.2°E / 44.4~52.4°N, 四周留出邻国与海域.
UKRAINE_EXTENT = BBox(21.0, 41.5, 43.2, 53.4)
# ETOPO 子集由画框外扩: 兰勃特投影画框四角向外张开, 反算到经纬度约 18.8~43.8°E /
# 42.7~53.25°N, 这里再留余量(数字与已下载的 16 MB NetCDF 一致, 不能随手改).
UKRAINE_ETOPO_BBOX = BBox(
    round(UKRAINE_EXTENT.lon_min - 2.5, 6),
    round(UKRAINE_EXTENT.lon_max + 2.5, 6),
    round(UKRAINE_EXTENT.lat_min - 0.8, 6),
    round(UKRAINE_EXTENT.lat_max + 0.1, 6),
)
UKRAINE_ETOPO_STRIDE = 2  # 15 弧秒数据按索引步长 2 抽样 = 30 弧秒
# OCHA HDX 直链: resources 页里的固定资源 id, 换版本时只改这一行.
UKRAINE_OCHA_URL = (
    "https://data.humdata.org/dataset/0a24205d-084c-4ef9-9d53-9349bacbde1e/"
    "resource/86861957-a064-4427-b93a-2277738f0caa/download/2021-hno-gca_ngca.zip"
)
UKRAINE_OCHA_ARCHIVE = "raw/2021-hno-gca_ngca.zip"
# 第聂伯河干流 relation(waterway=river), 以及梯级水库(从上游到下游)的 OSM relation id.
# 水库只取 outer 成员: inner 是水库里的岛(基辅水库一个 relation 就有 520 个 inner way),
# 图上不足 1 px.
UKRAINE_DNIEPER_RIVER = 2086905
UKRAINE_DNIEPER_RESERVOIRS: tuple[int, ...] = (
    1605938,  # 基辅水库
    3173129,  # 卡尼夫水库
    2289192,  # 克列缅丘格水库
    287177,  # 卡缅斯克水库
    21217234,  # 第聂伯水库
)
# 卡霍夫卡水库 2023-06 溃坝后 OSM 已删掉水面; 2022 口径用的是 NE 10m 湖泊层里的溃坝前水面.
UKRAINE_DNIESTER_RIVER = 2474595
UKRAINE_TRANSNISTRIA_RELATION = 65335
# 南布格河/顿河: NE 10m 在河口前收笔, 用 OSM 补下游段. bbox 是 (南, 西, 北, 东),
# 只覆盖收笔点到河口的一小段; 跨出 bbox 的 way 仍带完整几何, 足够接上.
UKRAINE_RIVER_EXTENSIONS: tuple[tuple[str, tuple[float, ...], str], ...] = (
    ("southern_bug", (46.85, 31.55, 47.40, 32.10), "osm-southern-bug"),
    ("don", (47.00, 39.05, 47.25, 39.60), "osm-don"),
)
# Overpass QL 按数据集 id 登记; 下载脚本从这里取查询体, 不在脚本里拼 bbox/relation.
UKRAINE_OSM_QUERIES: dict[str, str] = {
    "osm-dnieper-river": (
        f"[out:json][timeout:300];rel({UKRAINE_DNIEPER_RIVER});out geom;"
    ),
    "osm-dnieper-reservoirs": (
        "[out:json][timeout:300];rel(id:"
        + ",".join(str(rel_id) for rel_id in UKRAINE_DNIEPER_RESERVOIRS)
        + ')->.rels;way(r.rels:"outer");out geom;'
    ),
    "osm-dniester-river": (
        f"[out:json][timeout:300];rel({UKRAINE_DNIESTER_RIVER});out geom;"
    ),
    "osm-transnistria-boundary": (
        f"[out:json][timeout:300];rel({UKRAINE_TRANSNISTRIA_RELATION});out geom;"
    ),
}
for _key, _bbox, _cache_id in UKRAINE_RIVER_EXTENSIONS:
    _south, _west, _north, _east = _bbox
    UKRAINE_OSM_QUERIES[_cache_id] = (
        f'[out:json][timeout:120];way["waterway"="river"]'
        f"({_south},{_west},{_north},{_east});out geom;"
    )


def _ukraine(root: Path) -> DatasetSet:
    """乌克兰: ETOPO/GADM/OCHA/共享底图 + 河流/水库/德左边界的 Overpass 缓存."""
    etopo = Dataset(
        id="etopo-ukraine",
        kind="file",
        source_id="etopo-2022",
        url=erddap_url(
            "etopo-2022",
            stride=UKRAINE_ETOPO_STRIDE,
            lon_range=(UKRAINE_ETOPO_BBOX.lon_min, UKRAINE_ETOPO_BBOX.lon_max),
            lat_range=(UKRAINE_ETOPO_BBOX.lat_min, UKRAINE_ETOPO_BBOX.lat_max),
        ),
        dest=f"raw/ETOPO_2022_v1_15s_ukraine_stride{UKRAINE_ETOPO_STRIDE}.nc",
        files=("raw/ETOPO_2022_v1_15s_ukraine_stride*.nc",),
        bbox=UKRAINE_ETOPO_BBOX,
        stride=UKRAINE_ETOPO_STRIDE,
        note="ETOPO 2022 乌克兰区域高程(stride 2 = 30 弧秒)",
    )
    gadm = Dataset(
        id="gadm41-ukr",
        kind="file",
        source_id="gadm-4.1",
        url=gadm_url("UKR"),
        dest="raw/gadm41_UKR/gadm41_UKR_1.json",
        note="GADM 4.1 乌克兰一级行政区原始 JSON(法理口径, 含克里米亚/塞瓦斯托波尔)",
    )
    ocha = Dataset(
        id="ocha-gca-ngca",
        kind="zip",
        source_id="ocha-hno-2021",
        url=UKRAINE_OCHA_URL,
        dest="raw/ocha_gca_ngca",
        files=("raw/ocha_gca_ngca/*.shp",),
        note="OCHA/HDX 2021 HNO 顿巴斯 GCA/NGCA 分区(2021-hno-gca_ngca.zip)",
    )
    layers = ne_layers(
        (
            "ne-countries-10m",
            "10m/cultural/admin_0_countries",
            "Natural Earth 10m 国家多边形(海陆底色与国界轮廓)",
        ),
        (
            "ne-coastline-10m",
            "10m/physical/coastline",
            "Natural Earth 10m 海岸线(1991 国界剪掉沿海段的蒙版)",
        ),
        (
            "ne-borders-10m",
            "10m/cultural/admin_0_boundary_lines_land",
            "Natural Earth 10m 陆上国界(邻国参照线)",
        ),
        (
            "ne-rivers-10m",
            "10m/physical/rivers_lake_centerlines",
            "Natural Earth 10m 河网(一般河流与下游补段的 NE 上段)",
        ),
        (
            "ne-lakes-10m",
            "10m/physical/lakes",
            "Natural Earth 10m 湖泊(含溃坝前的卡霍夫卡水库水面)",
        ),
    )
    osm = (
        Dataset(
            id="osm-dnieper-river",
            kind="script",
            source_id="openstreetmap",
            dest="raw/osm_dnieper_river.json",
            note="第聂伯河干流 OSM 原始关系(缓存)",
        ),
        Dataset(
            id="osm-dnieper-reservoirs",
            kind="script",
            source_id="openstreetmap",
            dest="raw/osm_dnieper_reservoirs.json",
            note="第聂伯河梯级水库 OSM 原始 way(缓存)",
        ),
        Dataset(
            id="osm-dniester-river",
            kind="script",
            source_id="openstreetmap",
            dest="raw/osm_dniester_river.json",
            note="德涅斯特河干流 OSM 原始关系(缓存)",
        ),
        Dataset(
            id="osm-transnistria-boundary",
            kind="script",
            source_id="openstreetmap",
            dest="raw/osm_transnistria_boundary.json",
            note="德左(PMR)边界 OSM 原始关系(缓存)",
        ),
    )
    extensions = tuple(
        Dataset(
            id=cache_id,
            kind="script",
            source_id="openstreetmap",
            dest=f"raw/osm_{key}_lower.json",
            note=f"{key} 下游 OSM 原始 way(缓存)",
        )
        for key, _bbox, cache_id in UKRAINE_RIVER_EXTENSIONS
    )
    return dataset_set("乌克兰", root, [etopo, gadm, ocha, *layers, *osm, *extensions])


# ---- 伏尔加: ETOPO/GADM/HydroBASINS + 共享底图 ----
# ETOPO 下载范围由画框外扩: 画框 30.3~62.3°E / 44.4~62.3°N 是渲染规格, 四边各留 0.5°.
VOLGA_ETOPO_BBOX = BBox(29.8, 62.8, 43.9, 62.8)
VOLGA_HYBAS_MAX_LON_EU = 61.0


def _volga(root: Path) -> DatasetSet:
    """伏尔加: ETOPO 子集, GADM 俄罗斯一级行政区, HydroBASINS 5 级欧洲区."""
    etopo = Dataset(
        id="etopo-volga",
        kind="file",
        source_id="etopo-2022",
        url=erddap_url(
            "etopo-2022",
            stride=2,
            lon_range=(VOLGA_ETOPO_BBOX.lon_min, VOLGA_ETOPO_BBOX.lon_max),
            lat_range=(VOLGA_ETOPO_BBOX.lat_min, VOLGA_ETOPO_BBOX.lat_max),
        ),
        dest="raw/ETOPO_2022_v1_15s_volga_stride2.nc",
        files=("raw/ETOPO_2022_v1_15s_volga_stride*.nc",),
        bbox=VOLGA_ETOPO_BBOX,
        stride=2,
        note="ETOPO 2022 伏尔加河流域高程(stride 2 = 30 弧秒)",
    )
    gadm = Dataset(
        id="gadm41-rus",
        kind="file",
        source_id="gadm-4.1",
        url=gadm_url("RUS"),
        dest="raw/gadm41_RUS_1.json",
        note="GADM 4.1 俄罗斯一级行政区(原始)",
    )
    hybas = Dataset(
        id="hybas-eu-lev05",
        kind="zip",
        source_id="hydrobasins",
        url=hydrobasins_url("hybas_eu_lev05_v1c.zip"),
        dest="raw/hybas_eu_lev05_v1c",
        files=("raw/hybas_eu_lev05_v1c/hybas_eu_lev05_v1c.shp",),
        note=(
            "HydroBASINS level 5 欧洲区"
            "(下载 zip 并解压到 raw/hybas_eu_lev05_v1c/)"
        ),
    )
    layers = ne_layers(
        ("ne-rivers-10m", "10m/physical/rivers_lake_centerlines", "Natural Earth 10m 河网"),
        ("ne-lakes-10m", "10m/physical/lakes", "Natural Earth 10m 湖泊/水库水面"),
        (
            "ne-ocean-10m",
            "10m/physical/ocean",
            "Natural Earth 10m 海洋面(里海低地判识与流域面裁边)",
        ),
        (
            "ne-coastline-10m",
            "10m/physical/coastline",
            "Natural Earth 10m 海岸线(国界剪掉沿海段的蒙版)",
        ),
    )
    return dataset_set("伏尔加", root, [etopo, gadm, hybas, *layers])


# 区域顺序固定(与旧 manifest 的键顺序一致): manifest 的 groups 键序依赖它.
REGIONS = {
    "america_army": _america_army,
    "myanmar": _myanmar,
    "ukraine": _ukraine,
    "ussr": _ussr,
    "volga": _volga,
}


def region_names() -> tuple[str, ...]:
    """区域名(manifest 的 group 键, 也等于数据根下的目录名)."""
    return tuple(REGIONS)


def catalogs() -> list[tuple[str, DatasetSet]]:
    """按固定顺序取回各区域的 ``DatasetSet``."""
    root = data_root()
    return [(name, REGIONS[name](root / name)) for name in REGIONS]


# ============================
# manifest 构造与写出
# ============================
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "miko-gis-data/manifest"
# 字段/含义变更时加 1; 渲染仓按版本决定能不能读.
MANIFEST_VERSION = 1


def _source_payload(dataset: Dataset) -> dict | None:
    """数据集来源的静态事实; 渲染仓据它复算"数据来源"署名行."""
    parent = dataset.source
    if parent is None:
        return None
    return {
        "id": parent.id,
        "title": parent.title,
        "version": parent.version,
        "homepage": parent.homepage,
        "credit": parent.credit,
    }


def _item_payload(dataset: Dataset) -> dict:
    """一条数据集的 manifest 写法; 键顺序固定, 保证重复生成字节一致."""
    return {
        "id": dataset.id,
        "kind": dataset.kind,
        "note": dataset.note,
        "optional": dataset.optional,
        "dest": dataset.dest,
        "files": list(dataset.files),
        "version": dataset.version or "",
        "stride": dataset.stride,
        "bbox": dataset.bbox.as_dict() if dataset.bbox is not None else None,
        "resolution": dataset.resolution,
        "category": dataset.category,
        "name": dataset.name,
        "source": _source_payload(dataset),
    }


def build_manifest() -> dict:
    """构造整份清单(纯数据, 不写盘)."""
    groups: dict[str, dict] = {}
    for region, catalog in catalogs():
        groups[region] = {
            "title": catalog.group,
            "items": [_item_payload(item) for item in catalog],
        }
    return {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "groups": groups,
    }


def dump_manifest(payload: dict | None = None) -> str:
    """序列化成确定性文本: 键顺序由构造顺序固定, 结尾一个换行, 中文不转义."""
    payload = build_manifest() if payload is None else payload
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def manifest_path() -> Path:
    """清单的固定位置: 数据根根目录."""
    return data_root() / MANIFEST_NAME


def write_manifest() -> Path:
    """重建并写 ``<数据根>/manifest.json``; 返回写入路径."""
    payload = build_manifest()
    path = manifest_path()
    path.write_text(dump_manifest(payload), encoding="utf-8")
    print(f"已写清单: {path} ({len(payload['groups'])} 个区域)")
    return path


def manifest_is_current() -> bool:
    """磁盘上的清单是否与当前配置一致(用于 ``publish --check``, 不写盘)."""
    path = manifest_path()
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == dump_manifest()


# ============================
# check
# ============================
_STATE_LABELS = {
    "ok": "已存在",
    "missing": "缺失",
    "drifted": "版本不符",
    "optional": "可选缺失",
}


def check(group: str = "all", *, verbose: bool = True) -> int:
    """逐项核对原始数据(不联网); 返回失败项数量(0 表示齐全)."""
    selected = set(region_names() if group == "all" else [group])
    buckets = [(name, catalog) for name, catalog in catalogs() if name in selected]
    failed = 0
    total = 0
    for name, catalog in buckets:
        if verbose:
            print(f"== {name} / {catalog.group}({len(catalog)} 项)")
        for line in catalog.check():
            total += 1
            if line.failed:
                failed += 1
            if verbose:
                print(
                    f"  [{_STATE_LABELS.get(line.state, line.state)}] "
                    f"{line.dataset_id:<22s} {line.label:<34s} {line.detail}"
                )
    if verbose:
        print()
        if failed:
            print(
                f"共 {total} 项数据, {failed} 项缺失或版本不符. "
                "按下面命令准备(已就位的会自动跳过):"
            )
            print(f"  {SKILL_SCRIPT} download")
            print(f"  {SKILL_SCRIPT} publish")
        else:
            print(f"全部 {total} 项数据已就位, 可以直接到渲染仓出图, 不需要再下载.")
    return failed


# ============================
# download: 区域下载流程
# ============================
# Myanmar 的 --region 取值 -> 区域内部名(下载文件名与 glob 用它); CLI 用连字符,
# 文件名用下划线(渲染仓的 find_etopo 也按这个模板找文件).
MYANMAR_REGIONS = {"myanmar": "myanmar", "northern-shan": "northern_shan"}
# 区域内部名 -> 该区域登记的 ETOPO 数据集 id.
MYANMAR_ETOPO = {
    "myanmar": "etopo-myanmar",
    "northern_shan": "etopo-northern-shan",
}
# 文件名模板必须与渲染仓出图脚本的 find_etopo 约定一致, 否则出图时找不到刚下载的文件.
MYANMAR_DEST_TEMPLATE = "raw/ETOPO_2022_v1_15s_{region}_stride{stride}.nc"


def _download_myanmar(catalog: DatasetSet, args: argparse.Namespace) -> int:
    """缅甸: 先两套 ETOPO, 再掸邦 Overpass 分组, 最后行政区原始数据."""
    # all 子命令没有 --region(缅甸两套 ETOPO 总要都下), 这里按缺省 "all" 处理.
    region_arg = getattr(args, "region", "all")
    regions = (
        list(MYANMAR_ETOPO)
        if region_arg == "all"
        else [MYANMAR_REGIONS[region_arg]]
    )
    for region in regions:
        dataset = catalog.get(MYANMAR_ETOPO[region])
        stride = dataset.stride if args.stride is None else args.stride
        if stride is None or stride < 1:
            print(f"错误: --stride 必须 >= 1, 得到 {args.stride}", file=sys.stderr)
            return 1
        bbox = dataset.bbox
        if stride == dataset.stride:
            url, dest = dataset.url, catalog.path(dataset)
        elif bbox is None:
            print(f"错误: 数据集 {dataset.id} 没有登记 bbox", file=sys.stderr)
            return 1
        else:
            url = erddap_url(
                dataset.source_id,
                stride=stride,
                lon_range=(bbox.lon_min, bbox.lon_max),
                lat_range=(bbox.lat_min, bbox.lat_max),
            )
            dest = catalog.root / MYANMAR_DEST_TEMPLATE.format(
                region=region, stride=stride
            )
        print(f"下载 ETOPO 2022 {region} 区域高程 (数据集 {dataset.id})")
        print(url)
        fetch_file(url, dest, force=args.force)

    if args.skip_regions:
        print("已跳过掸邦特区边界/国界下载 (--skip-regions).")
    else:
        status = _download_myanmar_shan(catalog, force=args.force)
        if status != 0:
            return status
    return _download_myanmar_admin(catalog, force=args.force)


def _overpass_query(ids: tuple[int, ...] | list[int], bbox: tuple[float, ...] | None) -> bytes:
    """拼 Overpass QL 请求体; 给 bbox 时用 out geom(bbox) 只取范围成员几何."""
    out = (
        "out geom;"
        if bbox is None
        else "out geom(%s);" % ",".join(f"{v:g}" for v in bbox)
    )
    query = f"[out:json][timeout:170];relation(id:{','.join(map(str, ids))});{out}"
    return urllib.parse.urlencode({"data": query}).encode()


def _overpass_fetch(
    ids: tuple[int, ...] | list[int],
    dest: Path,
    *,
    description: str,
    bbox: tuple[float, ...] | None = None,
    force: bool = False,
) -> Path:
    """把一组 relation 的 Overpass 响应原样写到 dest; 失败轮换镜像重试.

    已存在且非空时跳过(公共实例有限流). 为什么轮换镜像: 实例满负荷时会直接 502 或提前
    断开 chunked 响应, 换一个实例比原地等待有效. 为什么要校验 elements: Overpass 出错时
    也返回 JSON(带 remark 而没有要素), 不校验就会把错误响应当原始数据缓存.
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"已存在, 跳过 {description}: {dest}")
        return dest

    body = _overpass_query(ids, bbox)
    endpoints = overpass_endpoints()
    attempts = 6
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        endpoint = endpoints[(attempt - 1) % len(endpoints)]
        try:
            post(endpoint, body, dest, force=force)
            payload = json.loads(dest.read_text(encoding="utf-8"))
            if not isinstance(payload.get("elements"), list):
                dest.unlink(missing_ok=True)
                raise RuntimeError("响应里没有 elements, 不是有效的 Overpass 结果")
            print(
                f"完成: {dest.name} "
                f"({dest.stat().st_size / 1024:.1f} KiB, {len(payload['elements'])} 元素)"
            )
            return dest
        except (
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            http.client.HTTPException,  # 实例负载高时提前断开 chunked 响应
            RuntimeError,  # wget 退出码非 0(含 502)与上面的 elements 校验
        ) as exc:
            # 解析失败时 post 已把坏响应落盘: 不删的话下一轮会看到"已存在, 跳过",
            # 反复校验同一份坏文件, 永远不会重新联网.
            dest.unlink(missing_ok=True)
            print(f"  请求失败 ({attempt}/{attempts}, {endpoint.split('/')[2]}): {exc}")
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Overpass 请求失败: {description}: {last_error}")


def _download_myanmar_shan(catalog: DatasetSet, *, force: bool) -> int:
    """抓掸邦各特区/国界的 Overpass 原始响应; 全部成功才写 .complete 标记.

    为什么需要标记: 下载是逐组的, 中途失败会留下半套 group_XX.json; 若用 group_*.json
    判定就位, 半套数据也会被报成"已存在". 本次若中断, 先撤标记让 check 如实报缺失.
    """
    group_dir = catalog.path(catalog.get("osm-shan-regions"))
    border_path = catalog.path(catalog.get("osm-country-borders"))
    marker = group_dir / ".complete"
    group_files = [group_dir / f"group_{index:02d}.json" for index in range(len(MYANMAR_GROUPS))]

    def complete() -> bool:
        if not marker.is_file():
            return False
        try:
            recorded = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        return recorded == len(MYANMAR_GROUPS) and all(p.is_file() for p in group_files)

    group_dir.mkdir(parents=True, exist_ok=True)
    border_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and complete() and border_path.is_file():
        print(f"已完成, 跳过 Overpass 下载: {group_dir} ({len(MYANMAR_GROUPS)} 组 + 国界已就位)")
        return 0
    marker.unlink(missing_ok=True)

    for index, group in enumerate(MYANMAR_GROUPS):
        _overpass_fetch(
            group,
            group_dir / f"group_{index:02d}.json",
            description=f"掸邦特区 relation 第 {index:02d} 组",
            force=force,
        )
    _overpass_fetch(
        MYANMAR_BORDER_RELATIONS,
        border_path,
        description="画框附近国界 relation",
        bbox=MYANMAR_BORDER_BBOX,
        force=force,
    )
    marker.write_text(f"{len(MYANMAR_GROUPS)}\n", encoding="utf-8")
    print(f"已写完成标记: {marker} ({len(MYANMAR_GROUPS)} 组)")
    return 0


def _download_myanmar_admin(catalog: DatasetSet, *, force: bool) -> int:
    """行政区图所需原始数据: GADM JSON, NE 河流 5.0.0 副本, 内比都 OSM 缓存."""
    gadm = catalog.get("gadm41-mmr")
    print("下载 GADM 4.1 缅甸一级行政区")
    fetch_file(gadm_url("MMR"), catalog.path(gadm), force=force)

    rivers = catalog.get("ne-rivers-10m")
    river_dir = catalog.path(rivers)
    river_shp = river_dir / "ne_10m_rivers_lake_centerlines.shp"
    if river_shp.is_file() and not force:
        print(f"已存在, 跳过 Natural Earth 10m 河流: {river_dir}")
    else:
        archive = river_dir.with_suffix(".zip")
        fetch_file(rivers.url, archive, force=force)
        extract_zip(archive, river_dir, force=force)
        found = catalog.version_of(rivers)
        if found and rivers.version and found != rivers.version:
            print(
                f"警告: 河流数据版本是 {found}, 期望 {rivers.version}; "
                "换版会改变河网几何, 请复核产物."
            )

    naypyidaw = catalog.get("osm-naypyidaw")
    query = f"[out:json][timeout:90];rel({NAYPYIDAW_RELATION_ID});out geom;"
    body = urllib.parse.urlencode({"data": query}).encode()
    last_error: Exception | None = None
    dest = catalog.path(naypyidaw)
    for endpoint in overpass_endpoints():
        try:
            post(endpoint, body, dest, force=force)
            # Overpass 出错时也返回 JSON(带 remark 而没有要素), 不校验就会把错误响应
            # 当原始数据缓存下来, 之后 check 还会报"已存在".
            payload = json.loads(dest.read_text(encoding="utf-8"))
            if not isinstance(payload.get("elements"), list):
                dest.unlink(missing_ok=True)
                raise RuntimeError("响应里没有 elements, 不是有效的 Overpass 结果")
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            dest.unlink(missing_ok=True)
            last_error = exc
            print(f"  Overpass 请求失败 ({endpoint}): {exc}")
    raise RuntimeError(f"无法从 Overpass 获取内比都联邦区边界: {last_error}")


def _download_ussr(catalog: DatasetSet, args: argparse.Namespace) -> int:
    """苏联: 只有 ETOPO 子集; 默认 stride 8(2 弧分), 文件名带实际步长."""
    stride = 8 if args.stride is None else args.stride
    if stride < 1:
        print(f"错误: --stride 必须 >= 1, 得到 {stride}", file=sys.stderr)
        return 1
    bbox = USSR_ETOPO_BBOX
    url = erddap_url(
        "etopo-2022",
        stride=stride,
        lon_range=(bbox.lon_min, bbox.lon_max),
        lat_range=(bbox.lat_min, bbox.lat_max),
    )
    dest = catalog.raw_dir / f"ETOPO_2022_v1_15s_ussr_stride{stride}.nc"
    print(f"下载 ETOPO 2022 苏联区域高程 (stride={stride})")
    print(url)
    fetch_file(url, dest, force=args.force)
    print("\n完成.数据就位后到渲染仓运行: python USSR/main.py render")
    return 0


def _overpass_data(dataset: Dataset, catalog: DatasetSet, query: str, *, force: bool) -> None:
    """执行一条 Overpass QL 并把原始 JSON 缓存到 dataset.dest; 实例依次轮换."""
    body = urllib.parse.urlencode({"data": query}).encode()
    dest = catalog.path(dataset)
    last_error: Exception | None = None
    for endpoint in overpass_endpoints():
        try:
            post(endpoint, body, dest, force=force)
            payload = json.loads(dest.read_text(encoding="utf-8"))
            if not isinstance(payload.get("elements"), list):
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"Overpass 返回异常(没有 elements): {endpoint}")
            print(
                f"完成: {dest.name} "
                f"({dest.stat().st_size / 1048576:.1f} MiB, "
                f"{len(payload['elements'])} 元素)"
            )
            return
        except (OSError, ValueError, RuntimeError) as exc:
            dest.unlink(missing_ok=True)
            last_error = exc
            print(f"  Overpass 失败({endpoint}): {exc}")
    raise RuntimeError(f"Overpass 下载失败: {dataset.note or dataset.id}: {last_error}")


def _download_ukraine(catalog: DatasetSet, *, force: bool) -> int:
    """乌克兰: ETOPO, GADM, OCHA, 共享底图提示, 6 份 OSM 原始响应."""
    print(
        f"下载 ETOPO 2022 乌克兰区域高程 "
        f"(stride={UKRAINE_ETOPO_STRIDE}, 约 30 弧秒)"
    )
    etopo = catalog.get("etopo-ukraine")
    print(f"  {etopo.url}")
    fetch_file(etopo.url, catalog.path(etopo), force=force)

    missing = [
        item
        for item in catalog
        if item.kind == "cartopy-ne" and not catalog.exists(item)
    ]
    for item in missing:
        print(
            f"警告: 缺少共享 Natural Earth 图层 "
            f"{item.resolution}/{item.category}/{item.name}; "
            f"请先运行: {SKILL_SCRIPT} download --group ukraine"
        )

    gadm = catalog.get("gadm41-ukr")
    print("下载 GADM 4.1 乌克兰一级行政区")
    fetch_file(gadm.url, catalog.path(gadm), force=force)

    ocha = catalog.get("ocha-gca-ngca")
    print("下载 OCHA/HDX 2021 HNO 顿巴斯 GCA/NGCA 分区")
    if catalog.exists(ocha) and not force:
        # 解压目录已就位时连压缩包都不碰: 删掉中间 zip 不该触发重新联网.
        print(f"已存在, 跳过 {ocha.note}: {catalog.path(ocha)}")
    else:
        archive = catalog.root / UKRAINE_OCHA_ARCHIVE
        fetch_file(ocha.url, archive, force=force)
        extract_zip(archive, catalog.path(ocha), force=force)

    print("下载 OpenStreetMap 河流与边界原始关系 (Overpass)")
    for item in catalog:
        if item.kind == "script" and item.source_id == "openstreetmap":
            _overpass_data(item, catalog, UKRAINE_OSM_QUERIES[item.id], force=force)

    failures = sum(1 for line in catalog.check() if line.failed)
    if failures:
        print(f"\n警告: 仍有 {failures} 项数据未就位, 可重跑本命令补齐.", file=sys.stderr)
        return 1
    print("\n原始数据下载完成.")
    return 0


def _shapefile_bbox(path: Path) -> tuple[float, float, float, float]:
    """读 shapefile 头 100 字节里的 bbox, 返回 (西, 南, 东, 北).

    为什么手读头部: 本技能只做下载/解压, 依赖只有标准库; 覆盖范围检查只要头里的四个
    double(小端, 偏移 36), 不需要解析记录内容.
    """
    with open(path, "rb") as fh:
        header = fh.read(100)
    if len(header) < 100 or struct.unpack(">i", header[0:4])[0] != 9994:
        raise RuntimeError(f"不是有效的 shapefile(头部校验失败): {path}")
    west, south, east, north = struct.unpack("<4d", header[36:68])
    return west, south, east, north


def _drop_shapefile_index(out_dir: Path) -> None:
    """删掉 shp 附带的 .sbn/.sbx 空间索引: HydroBASINS v1c 的 .sbn 与 GDAL 不兼容
    (带 bbox 过滤读取时报 ``Invalid node descriptor size in .sbn``), 删掉不影响几何.
    """
    for pattern in ("*.sbn", "*.sbx"):
        for path in out_dir.glob(pattern):
            path.unlink(missing_ok=True)


def _download_volga(catalog: DatasetSet, *, force: bool, skip_basin: bool) -> int:
    """伏尔加: ETOPO, GADM, HydroBASINS(下载 zip 并解压)."""
    print("\n[1/3] 下载 ETOPO 2022 高程")
    etopo = catalog.get("etopo-volga")
    print(f"  {etopo.url}")
    fetch_file(etopo.url, catalog.path(etopo), force=force)

    print("\n[2/3] 下载 GADM 4.1 俄罗斯一级行政区")
    gadm = catalog.get("gadm41-rus")
    fetch_file(gadm.url, catalog.path(gadm), force=force)

    if skip_basin:
        print("\n--skip-basin: 跳过 HydroBASINS 下载")
    else:
        print("\n[3/3] 下载并解压 HydroBASINS level 5")
        # 单列 try: HydroBASINS 源偶尔不可达时, ETOPO/GADM 仍已就位, 把缺失留给 check.
        try:
            hybas = catalog.get("hybas-eu-lev05")
            archive = catalog.root / "raw/hybas_eu_lev05_v1c.zip"
            fetch_file(hybas.url, archive, force=force)
            if force or not catalog.exists(hybas):
                extract_zip(archive, catalog.path(hybas), force=force)
            else:
                print(f"已存在, 跳过解压: {catalog.path(hybas)}")
            _drop_shapefile_index(catalog.path(hybas))
            west, south, east, north = _shapefile_bbox(catalog.path(hybas))
            print(f"   eu lev05 bbox: {west:.2f}~{east:.2f}°E / {south:.2f}~{north:.2f}°N")
            if east < VOLGA_HYBAS_MAX_LON_EU:
                print(
                    f"   警告: eu 区东界 {east:.2f}°E < {VOLGA_HYBAS_MAX_LON_EU}°E, "
                    "伏尔加流域东缘可能缺单元; 需要补 as 区时先改 REGIONS 里的数据集"
                )
        except Exception as exc:
            print(f"\n警告: HydroBASINS 下载失败: {exc}")
            print("       可稍后重跑(原始文件会从 .part 断点续传), 或加 --skip-basin")

    failures = sum(1 for line in catalog.check() if line.failed)
    if failures:
        print(f"\n警告: 仍有 {failures} 项数据未就位, 可重跑本命令补齐.", file=sys.stderr)
        return 1
    print("\n原始数据下载完成.")
    return 0


def _selected(group: str) -> list[str]:
    """把 --group 解析成有序区域名列表."""
    return list(region_names()) if group == "all" else [group]


def _download_group(group: str, args: argparse.Namespace) -> int:
    """下载一个区域的原始数据; 返回退出码. 共享底图不在这里处理."""
    catalog = dict(catalogs())[group]
    if group == "myanmar":
        return _download_myanmar(catalog, args)
    if group == "ussr":
        return _download_ussr(catalog, args)
    if group == "ukraine":
        return _download_ukraine(catalog, force=args.force)
    if group == "volga":
        return _download_volga(catalog, force=args.force, skip_basin=args.skip_basin)
    # america_army 只有共享 Natural Earth 图层, 由 download 统一预取.
    print(f"{group}: 没有自有数据, 只需共享 Natural Earth 图层(已统一预取).")
    return 0


def download(group: str = "all", args: argparse.Namespace | None = None) -> int:
    """预取共享底图 + 下载选中区域的原始数据; 返回退出码."""
    args = args or build_parser().parse_args(["download"])
    all_catalogs = [catalog for _name, catalog in catalogs()]
    failures = fetch_shared_layers(all_catalogs)
    for name in _selected(group):
        status = _download_group(name, args)
        if status != 0:
            return status
    if failures:
        print(f"共享 Natural Earth 图层失败 {failures} 个.", file=sys.stderr)
        return 1
    return 0


# ============================
# publish
# ============================
def publish(*, check: bool = False) -> int:
    """写 manifest 或只校验它; 返回 0=成功/一致."""
    if check:
        if manifest_is_current():
            print(f"清单与当前配置一致: {manifest_path()}")
            return 0
        print(
            f"清单缺失或与配置不一致, 请运行: {SKILL_SCRIPT} publish",
            file=sys.stderr,
        )
        return 1
    write_manifest()
    return 0


# ============================
# 命令行
# ============================
def build_parser() -> argparse.ArgumentParser:
    """构造命令行: check / download / publish / all 四个子命令."""
    parser = argparse.ArgumentParser(
        prog="gis_data.py",
        description="地图原始数据: 检查,下载/解压,发布 manifest.json.",
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get(DATA_ROOT_ENV, str(DEFAULT_DATA_ROOT)),
        help=(
            "数据根(manifest.json 与各区域数据的存放处); "
            f"默认 {DEFAULT_DATA_ROOT}, 也可用环境变量 {DATA_ROOT_ENV}"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    check_cmd = subparsers.add_parser("check", help="逐项核对数据是否就位且版本相符(不联网)")
    check_cmd.add_argument(
        "--group", choices=("all", *region_names()), default="all", help="只检查一个区域"
    )

    download_cmd = subparsers.add_parser("download", help="预取共享底图并下载原始数据")
    download_cmd.add_argument(
        "--group", choices=("all", *region_names()), default="all", help="只下载一个区域"
    )
    download_cmd.add_argument(
        "--region",
        choices=("all", *MYANMAR_REGIONS),
        default="all",
        help="仅 Myanmar: ETOPO 下载区域; all=缅甸全境与掸邦都下",
    )
    download_cmd.add_argument(
        "--stride",
        type=int,
        default=None,
        help="ETOPO 类区域: 覆盖默认采样步长(缅甸用登记值, 苏联缺省 8)",
    )
    download_cmd.add_argument("--force", action="store_true", help="覆盖已存在的下载文件")
    download_cmd.add_argument(
        "--skip-regions",
        action="store_true",
        help="仅 Myanmar: 跳过掸邦特区边界/国界下载(其余照常)",
    )
    download_cmd.add_argument(
        "--skip-basin",
        action="store_true",
        help="仅 Volga: 跳过 HydroBASINS 下载(只准备 ETOPO 与 GADM)",
    )

    publish_cmd = subparsers.add_parser("publish", help="重建数据清单 manifest.json")
    publish_cmd.add_argument(
        "--group",
        choices=("all", *region_names()),
        default="all",
        help="接受但忽略, 清单总是整体生成",
    )
    publish_cmd.add_argument(
        "--check", action="store_true", help="只检查清单是否与当前配置一致, 不写盘"
    )

    all_cmd = subparsers.add_parser("all", help="依次 download -> publish -> check")
    all_cmd.add_argument(
        "--group", choices=("all", *region_names()), default="all", help="只处理一个区域"
    )
    all_cmd.add_argument("--stride", type=int, default=None, help="ETOPO 类区域的采样步长")
    all_cmd.add_argument("--force", action="store_true", help="覆盖已存在的下载文件")
    all_cmd.add_argument(
        "--skip-regions", action="store_true", help="仅 Myanmar: 跳过掸邦区域数据"
    )
    all_cmd.add_argument(
        "--skip-basin", action="store_true", help="仅 Volga: 跳过 HydroBasins"
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    """解析命令行并分派; 返回进程退出码(0 成功)."""
    args = build_parser().parse_args(argv)

    global ROOT
    ROOT = Path(args.data_root).expanduser().resolve()

    if args.command == "check":
        return 0 if check(args.group) == 0 else 1

    if args.command == "publish":
        return publish(check=args.check)

    if args.command == "download":
        return download(args.group, args)

    # all: download -> publish -> check, 任一步失败就停.
    status = download(args.group, args)
    if status != 0:
        return status
    write_manifest()
    return 0 if check(args.group) == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """入口包装: 捕获中断与异常, 统一退出码."""
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\n用户中断.可重新运行以续传未完成的下载.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 入口统一收口, 打印中文提示
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
