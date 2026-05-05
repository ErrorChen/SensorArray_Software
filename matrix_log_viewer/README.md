# Matrix Log Viewer

Matrix Log Viewer 用于从串口实时读取检测矩阵输出的 `MATV` 日志，并把 8x8 检测矩阵显示成可点击的热力图。每个格子显示点位名和当前数值，点击任一点位后，右侧曲线图会显示该点位随时间变化的历史数据。

## 安装

```powershell
cd matrix_log_viewer
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Linux/macOS 激活虚拟环境：

```bash
source .venv/bin/activate
```

## 运行

从项目根目录打开图形化启动器：

```powershell
python main.py
```

也可以在 `matrix_log_viewer` 目录内直接运行启动器：

```powershell
python run_gui.py
```

启动器可以选择串口或历史日志文件，并一键打开浏览器图形界面。

Windows 串口实时读取示例：

```powershell
python run_viewer.py --port COM5 --baud 115200
```

Linux/macOS 串口示例：

```bash
python run_viewer.py --port /dev/ttyUSB0 --baud 115200
```

历史日志重放示例：

```powershell
python run_viewer.py --replay-file sample_logs/sample_matv.log --replay-speed 10
```

启动后在浏览器打开：

```text
http://127.0.0.1:8050
```

## 参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--port` | 无 | 串口名称，例如 `COM5`、`/dev/ttyUSB0`、`/dev/ttyACM0`。使用 `--replay-file` 时不需要。 |
| `--baud` | `115200` | 串口波特率。 |
| `--max-points` | `5000` | 每个点位最多保留的历史点数。 |
| `--save-csv` | 无 | 可选路径。每解析到一帧就追加保存到 CSV。 |
| `--replay-file` | 无 | 从历史日志文件读取，而不是打开串口。 |
| `--replay-speed` | `1.0` | 历史日志重放速度倍率，`10.0` 表示快 10 倍。 |
| `--host` | `127.0.0.1` | Dash 服务监听地址。 |
| `--port-web` | `8050` | Dash 服务端口。 |
| `--debug` | 关闭 | 开启 DEBUG 日志。 |

`--port` 和 `--replay-file` 至少提供一个。提供 `--replay-file` 时程序不会打开串口。

## 日志格式

程序会自动忽略非 `MATV_HEADER` 和非 `MATV` 行，例如 `DBG`、`INIT`、`ERROR`、`WARN`。空行、乱码行、残缺行、字段数量不一致行会被安全跳过，并在页面状态栏显示跳过数量和解析错误数量。

Header 示例：

```text
MATV_HEADER,seq,timestamp_us,duration_us,unit,S1D1,S1D2,...,S8D8
```

数据行示例：

```text
MATV,8200,512700622,31386,uV,53092,-45237,...,-68588
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| 第 0 列 | 固定字符串 `MATV`。 |
| `seq` | 帧序号。 |
| `timestamp_us` | ESP32 侧时间戳，单位微秒。 |
| `duration_us` | 一次矩阵扫描持续时间，单位微秒。 |
| `unit` | 数值单位，例如 `uV`、`mV`、`V`、`pF`、`raw`。 |
| `S1D1` 到 `S8D8` | 64 个检测矩阵点位读数。 |

如果日志中缺少 `MATV_HEADER`，程序会使用默认顺序解析：

```text
S1D1,S1D2,...,S1D8,S2D1,...,S8D8
```

## 界面功能

- 8x8 热力图实时刷新，行是 `S1` 到 `S8`，列是 `D1` 到 `D8`。
- 每个格子显示点位名和当前值。
- 点击热力图格子后，历史曲线自动切换到该点位。
- 右侧 `Cell` 下拉框可以直接切换不同点位的历史曲线。
- `Pause / Resume` 可以暂停或继续从队列读取并刷新数据。
- `Clear History` 清空内存历史数据，当前帧计数归零。
- `Save Snapshot CSV` 将当前内存宽表数据保存到运行目录的 `exports/` 文件夹。
- 色阶支持 `Auto`、`Symmetric around zero`、`Fixed range`。
- 显示单位支持 `Auto uV/mV/V`、`Source unit`、`uV`、`mV`、`V`。单位转换只影响界面显示和图表，不改变内存原始值或 CSV 导出值。

## CSV 输出

启动时使用 `--save-csv path` 可以实时追加保存解析后的帧：

```powershell
python run_viewer.py --port COM5 --save-csv logs\matv_capture.csv
```

CSV 表头：

```text
seq,timestamp_us,time_s,duration_us,unit,S1D1,...,S8D8
```

如果文件已存在，程序会继续追加；如果文件不存在或为空，会先写入表头。

## 常见问题

**COM 口打不开**

检查串口是否被 VSCode serial monitor、`idf.py monitor`、Arduino IDE 或其他程序占用。关闭占用程序后，viewer 会每隔 1 秒尝试重连。

**没有数据显示**

确认固件正在输出 `MATV` 行，并确认波特率和串口号正确。

**热力图全空**

检查 `MATV_HEADER` 和 `MATV` 字段数量。完整数据行应至少有 `5 + 64 = 69` 个字段。

**数值颜色不明显**

切换到 `Symmetric around zero`，或使用 `Fixed range` 手动设置色阶范围。

**串口中途断开**

程序不会崩溃，页面会显示 `Disconnected`，并每隔 1 秒尝试重新打开串口。恢复连接后会继续读取。
