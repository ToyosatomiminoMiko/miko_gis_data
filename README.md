# miko-gis-data

DSH 技能: 下载, 校验并发布地图原始数据与清单 `manifest.json`, 供渲染仓
[`Cartopy_file`](https://github.com/ToyosatomiminoMiko/Cartopy_file) 离线读取底图与区域数据.

整个技能只有一个 Python 脚本, 只依赖标准库; 系统侧依赖 `wget` (GNU Wget 2.x 或 1.20+)
与 `unzip`. 只有 `download` / `all` 会联网, `check` 与 `publish` 完全离线.

## 安装

技能目录固定为 `~/.dsh/skills/miko-gis-data` (DSH 从这里发现技能):

```sh
git clone https://github.com/ToyosatomiminoMiko/miko_skill_gisdata.git ~/.dsh/skills/miko-gis-data
```

## 命令

```sh
python3 ~/.dsh/skills/miko-gis-data/gis_data.py check     # 不联网核对 40 项数据是否就位
python3 ~/.dsh/skills/miko-gis-data/gis_data.py download  # 预取共享底图 + 下载/解压各区域数据
python3 ~/.dsh/skills/miko-gis-data/gis_data.py publish   # 重建 <数据根>/manifest.json
python3 ~/.dsh/skills/miko-gis-data/gis_data.py publish --check   # 只校验清单一致性
python3 ~/.dsh/skills/miko-gis-data/gis_data.py all       # download -> publish -> check
```

`download` 已存在的文件会跳过; `--group` 限定单个区域, 其余选项见 `SKILL.md` 或 `--help`.

## 数据根

默认 `~/miko_gis_data/data`, 可用 `--data-root PATH` 或环境变量 `MIKO_GIS_DATA_ROOT`
覆盖. 数据根里是下载原样的原始文件与 `<数据根>/manifest.json`; 数据本身不进本仓库.

## 仓库内容

| 文件 | 说明 |
| --- | --- |
| `SKILL.md` | 技能说明: 命令, 数据根契约, 数据源与许可, 区域清单, manifest 格式, 已知坑 |
| `gis_data.py` | 唯一下载脚本 (标准库; 调用外部 `wget`/`unzip`) |
| `LICENSE` | GPL-3.0 |

区域: `america_army`, `myanmar`, `ukraine`, `ussr`, `volga`; 数据源: Natural Earth
5.1.1, NOAA ETOPO 2022, GADM 4.1, HydroBASINS v1c, OCHA HNO 2021, OpenStreetMap (ODbL).
