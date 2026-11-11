---
name: miko-gis-data
description: 下载,校验并发布地图原始数据与 manifest.json, 供 Cartopy_file 渲染仓离线读取底图与区域数据.
whenToUse: 当需要准备或补全地图原始数据, 处理数据缺失/版本不符/manifest 过期, 或新增区域与数据源时使用.
---

# miko-gis-data: 地图原始数据的下载与发布

本技能把外部数据源变成数据根里的原始文件, 校验就位与版本, 并发布 `<数据根>/manifest.json`
给渲染仓 `Cartopy_file` 只读消费. 只做下载, 入库与解压, 不做派生 GeoJSON, 不做出图.

## 命令

脚本: `~/.dsh/skills/miko-gis-data/gis_data.py`. 仅用 Python 标准库; 系统依赖 `wget`
(GNU Wget 2.x 或 1.20+) 与 `unzip`.

```sh
python3 ~/.dsh/skills/miko-gis-data/gis_data.py [--data-root PATH] <子命令> [选项]

check                    # 逐项核对原始数据是否就位且版本相符, 不联网
download                 # 预取共享 Natural Earth 图层 + 下载/解压各区域原始数据(已存在则跳过)
publish                  # 重建 <数据根>/manifest.json
publish --check          # 只校验清单: 一致退出 0, 缺失或过期退出 1
all                      # 依次 download -> publish -> check, 任一步失败就停
```

选项:

- `--data-root PATH`: 数据根; 未给时读环境变量 `MIKO_GIS_DATA_ROOT`, 再缺省用
  `~/miko_skill_gisdata/data`.
- `--group {all,america_army,myanmar,ussr,ukraine,volga}`: 只处理一个区域; `publish` 接受但
  忽略它, 清单总是整份生成.
- `download` 追加: `--region {all,myanmar,northern-shan}` 选缅甸 ETOPO 子集; `--stride N`
  覆盖 ETOPO 索引步长; `--force` 重下已存在文件; `--skip-regions` 缅甸跳过掸邦特区/国界;
  `--skip-basin` 伏尔加跳过 HydroBASINS.

常用:

```sh
# 首次或换机器: 一条命令走完(联网只发生在这里)
python3 ~/.dsh/skills/miko-gis-data/gis_data.py all

# 只补缅甸, 先看缺什么
python3 ~/.dsh/skills/miko-gis-data/gis_data.py check --group myanmar
python3 ~/.dsh/skills/miko-gis-data/gis_data.py download --group myanmar --skip-regions
python3 ~/.dsh/skills/miko-gis-data/gis_data.py publish
```

## 数据根与渲染仓耦合

```text
<数据根>/manifest.json                                            两仓唯一契约
<数据根>/cartopy/shapefiles/natural_earth/<category>/ne_<res>_<name>.*   共享 Natural Earth 缓存
<数据根>/<region>/raw/                                            下载原样(单文件/zip/解压目录)
```

- 数据根默认 `~/miko_skill_gisdata/data`; 换位置用 `--data-root` 或 `MIKO_GIS_DATA_ROOT`.
- 渲染仓 `Cartopy_file` 在仓根 `config.toml` 写 `data_root = "<数据根>"`, 只读
  `manifest.json` 与它指向的文件, 不 import 本技能, 不写回数据根.
- 清单只含相对 `<数据根>/<region>/` 的路径, 不含时间戳与绝对路径, 所以数据根可整体搬运;
  搬完只改渲染仓那一行 `data_root`.
- 数据不进 git; 换机器重跑 `all` 即可恢复.

## 数据源与许可

URL 模板, 版本, 许可与署名只在 `gis_data.py` 的 `SOURCES` 登记一次, 区域数据集只引用 id.

| id | 数据集 | 版本 | 许可 | 进图署名 |
| --- | --- | --- | --- | --- |
| `natural-earth` | Natural Earth(10m/110m) | 5.1.1 | public domain | `Natural Earth 5.1.1` |
| `etopo-2022` | NOAA ETOPO 2022(15 弧秒) | v1 (15 arc-second) | public domain (US Government work) | `NOAA ETOPO 2022 (15″)` |
| `gadm-4.1` | GADM 一级行政区 | 4.1 | 学术与非商业免费, 商用需授权 | `GADM 4.1` |
| `hydrobasins` | HydroBASINS(HydroSHEDS) | v1c | 免费, 需按发布页署名 | `HydroBASINS (HydroSHEDS) v1c` |
| `ocha-hno-2021` | OCHA HDX 2021 HNO GCA/NGCA | 2021 | 按 HDX 数据集页 | `OCHA 2021 HNO` |
| `openstreetmap` | OpenStreetMap(Overpass 快照) | 无版本号 | ODbL 1.0 | `(C) OpenStreetMap contributors (ODbL)` |

- 产物署名按 source 去重; 同一来源版本不一致时并排写出版本(如缅甸河网钉 5.0.0).
- 引用数据时以 `homepage`(发布页)为准, 不要用 S3/ERDDAP 这类会随参数变化的下载地址.

## 区域与产物

`--group` 取值即 manifest 里 `groups` 的键; 每个区域的数据都只在 `<数据根>/<region>/raw/` 下.

| group / 标题 | 产物 |
| --- | --- |
| `america_army` / 美军基地(6 项) | 无自有数据, 只有共享 Natural Earth 110m 6 层(land/ocean/coastline/borders/lakes/rivers). |
| `myanmar` / 缅甸(10 项) | 两套 ETOPO: 全境 stride 2(`raw/ETOPO_2022_v1_15s_myanmar_stride2.nc`)与掸邦放大图 stride 1; GADM `raw/gadm41_MMR/gadm41_MMR_1.json`; NE 10m 国家/海岸线/陆上国界共享层; NE 10m 河流 5.0.0 独立副本 `raw/ne_10m_rivers_lake_centerlines/`; 掸邦 7 组 Overpass 原始响应 `raw/regions/osm_shan_relations/group_00..06.json` + `.complete`; 国界响应 `raw/regions/osm_country_borders.json`; 内比都缓存 `raw/osm_naypyidaw_union_territory.json`. |
| `ussr` / 苏联(3 项) | ETOPO 子集 stride 8(`raw/ETOPO_2022_v1_15s_ussr_stride8.nc`), 范围 15~195°E / 20~90°N; NE 10m 国家多边形 + 110m 海岸线. |
| `ukraine` / 乌克兰(14 项) | ETOPO stride 2; GADM `raw/gadm41_UKR/gadm41_UKR_1.json`; OCHA 压缩包 `raw/2021-hno-gca_ngca.zip` 与解压目录 `raw/ocha_gca_ngca/`; 5 个 NE 10m 层(国家/海岸线/陆上国界/河流/湖泊); 6 份 OSM 原始 JSON: 第聂伯河/梯级水库/德涅斯特河/德左边界/南布格下游/顿河下游. |
| `volga` / 伏尔加(7 项) | ETOPO stride 2; GADM `raw/gadm41_RUS_1.json`; HydroBASINS eu lev05 的 zip 与解压目录 `raw/hybas_eu_lev05_v1c/`; NE 10m 河流/湖泊/海洋/海岸线. |

- 流域, 行政区, 特区, 国界, 河道等派生 GeoJSON 全部由渲染仓从这些原始文件生成, 本技能不保存.
- 跨仓约定的数据集 id 与 `dest` 是渲染仓的读取口径, 不要单边改; 要改先两仓对齐.

## manifest 契约

`<数据根>/manifest.json` 是两仓唯一契约. 顶层与 item 形状固定:

```json
{
  "format": "miko-gis-data/manifest",
  "version": 1,
  "groups": {
    "<region>": {
      "title": "中文分组名",
      "items": [
        {
          "id": "数据集 id", "kind": "file", "note": "人读说明", "optional": false,
          "dest": "raw/...", "files": ["raw/..."], "version": "",
          "stride": 2, "bbox": {"lon_min": 0, "lon_max": 0, "lat_min": 0, "lat_max": 0},
          "resolution": "", "category": "", "name": "",
          "source": {"id": "", "title": "", "version": "", "homepage": "", "credit": ""}
        }
      ]
    }
  }
}
```

- `kind` 只有 `file` / `zip` / `cartopy-ne` / `script` / `local`, 没有派生类型.
- item 键序固定为上面顺序; `dest`/`files` 相对 `<数据根>/<region>/`; `files` 是就位判定
  glob, 空数组表示用 `dest`; `bbox` 为 `{lon_min, lon_max, lat_min, lat_max}` 或 null.
- `cartopy-ne` 的 `dest` 为空串, 图层在共享缓存, 按 `resolution`/`category`/`name` 定位;
  `version` 为空串时实际版本取 `source.version`; `source` 为 null 表示项目自带数据.
- `optional=true` 是可选缓存, 缺失不算失败.
- `publish` 输出必须确定性: 区域按固定顺序(`america_army`, `myanmar`, `ukraine`, `ussr`,
  `volga`), item 按声明顺序, 固定键序, 无时间戳/绝对路径, 中文不转义, `indent=2` 且结尾
  一个换行; 同配置同数据重复发布逐字节一致.
- 不要手改 `manifest.json`; 改字段含义要升 `version` 并同步渲染仓的读取端.
- `publish` 不检查数据是否就位; 缺什么由 `check` 报.

## 关键约定与坑

1. ERDDAP 经度是 0~360°: 跨 180° 的区域要把负经度 +360(苏联写 15~195°E, 不写
   15~-165°E). `stride` 是索引步长, 不是角度: 15 弧秒数据 stride 2 = 30 弧秒,
   stride 8 = 2 弧分.
2. 下载范围必须盖住投影后矩形画框的四角, 不能只按经纬度画框: 兰勃特/圆锥投影的矩形
   画框四角会向外张开, 范围不够时图角露出无高程空白. 数字由画框外扩得到, 与渲染仓
   画框同源; 渲染侧还会核对覆盖, 已下载的缓存不能随手改范围.
3. 续传前必须探测服务器对 `Range` 是否返回 206; ERDDAP 动态子集不认 Range, wget 会把
   完整 200 响应接在旧 `.part` 后面且退出码仍是 0, 拼出坏文件. 不支持时删 `.part` 从零下载.
4. HTTP 200 不等于成功: 下载后按落点后缀校验文件头(`.nc` 要 `CDF`/`\x89HDF`, `.json`
   要 `{`/`[`, `.zip` 要 `PK`), 不符就当错误页删掉. 后缀必须反映真实内容, 无后缀缓存
   不校验.
5. `.part` 只是半成品: 失败会留下, 下次续传或从零重下; 不要把它当数据, 不要进 git 或清单.
6. 共享 Natural Earth 缓存不响应 `--force`: 要换版或重下, 先手动删目标
   `ne_<res>_<name>.*`, 再 `download`; 换版属于要单独评审的改动.
7. 版本靠证据文件逐字比对: Natural Earth 的 S3 地址不带版本且换版会原地覆盖, 版本以缓存里
   `*.VERSION.txt` 第一行为准(当前 5.1.1); 缅甸河流用的 5.0.0 副本单独钉在数据集上. 版本
   不符是失败不是警告.
8. Overpass 只缓存原始响应: 查询体按区域登记, 响应里有 `elements` 才算有效. 缅甸掸邦分 7 组
   下载, 全部组与国界都成功后才写 `.complete` 完整性标记, 就位判定只认这个标记; 缺组时
   重跑会撤标记并补齐. 公共实例失败按镜像轮换重试, 重新抓要删缓存或 `--force`.
9. 解压属于下载步骤: OCHA 与 HydroBASINS 的 zip 先落 `raw/`, 再解压到 `dest` 目录;
   已解压用 `.unpacked` 标记跳过. HydroBASINS v1c 的 `.sbn`/`.sbx` 与 GDAL 不兼容
   (bbox 过滤会报 `Invalid node descriptor size in .sbn`), 解压后删除. 该 eu 区东界
   小于 61°E 时会告警伏尔加东缘可能缺单元, 补 as 区属于改数据源.
10. 联网只发生在 `download`(含 ERDDAP 与 Overpass); `check` 与 `publish` 全离线.
11. 数据集 `optional` 只给缺了不影响出图的中间缓存, 不要用它掩盖必需数据.
12. GADM/OCHA/HydroBASINS 换链接或版本时, 改 `SOURCES`(OCHA 的直链在乌克兰区域登记旁的
    `UKRAINE_OCHA_URL`)并重新 `publish`, 否则渲染仓读到的版本与署名会和实际数据不一致.

## 新增区域或数据源

全部在 `gis_data.py` 内完成, 没有 per-region 的 `config.py`.

- 新增区域: 在 `REGIONS` 加一项, 至少给出区域名(即 manifest 的 group 键), 中文标题,
  数据集列表, 以及下载/校验所需参数(ETOPO 范围与 stride, Overpass 查询或脚本行为).
- 新增数据源: 在 `SOURCES` 加一条, 填 `id`, `title`, `version`, URL 模板, `homepage`,
  `license`, `credit`; 数据集用 `source_id` 引用它. URL,版本,许可,署名不写在区域项里.
- 数据集字段: `id`(区域内唯一), `kind`, `source_id`, `url`, `dest`, `files`, `version`,
  `bbox`, `stride`, `optional`, `note`. 共享 Natural Earth 图层按
  `resolution/category/name` 登记, 由 `download` 统一去重预取.
- 改完按顺序验证: `download` -> `publish` -> `check`, 再跑 `publish --check`.
- 清单里的 group 键与 item id/dest 是渲染仓的 API, 新增或改名要两仓对齐.

## 自检

```sh
S=~/.dsh/skills/miko-gis-data/gis_data.py
python3 "$S" publish --check   # 清单是否与当前配置逐字一致
python3 "$S" check             # 全部区域就位且版本相符
python3 "$S" check --group ukraine
```

- `check` 逐项报告已就位/缺失/版本不符/可选缺失; 缺失与版本不符算失败, 退出码 1.
- `publish --check` 不一致时只读不写, 退出码 1, 按提示重跑 `publish`.
- 发布后到渲染仓 `Cartopy_file` 出图; 数据换版或补数据后还要在那边重跑 `prepare` 并复核
  产物字节.

## 范围外

- 派生 GeoJSON(裁剪, 补缝, 多边形化), 投影, 色带, 图例, 地名与出图都在渲染仓
  `Cartopy_file`, 由它的 `prepare`/render 完成, 产物不写回数据根.
- 本技能不 import cartopy/matplotlib/GIS 库, 不生成任何派生文件.
- 数据换版会改变渲染产物; 那属于两仓同步改动, 不能只在数据侧改期望值.
