# 启明泵房声学巡检站

站端以 Python 3.11 标准库和 SQLite 管理一台录制仪的采集及交班。早班录制，夜班取票并按页核对，白班可在设备停止后继续领取。每个样本的原始整数振幅、设备发生号、原序和原槽可核对。物理槽覆写后只保留新内容，历史已接纳版本仍能按原身份取件。

## 核验与演练

```sh
python3 -B check_project.py
python3 -B -m unittest discover
python3 -B -m survey.recording.station demo
python3 -B -m examples.overwrite_interleave demo
python3 -B -m examples.overwrite_interleave verify
```

`check_project.py` 由运行平台在物化项目时提供。演练从 `examples/workday.json` 读取固定事件，在临时站库采集 7、11，签出夜班票并领取 7，然后将第二槽换成 19 并发白班票。关源重开后，夜班票从偏移 1 取得原帧 11，白班票从偏移 0 取得 7、19。两票的冻结峰值分别为 11、19。命令输出交付页、出处和离线核账结果。

## 站端接口

用 `survey.recording.station.connect_station(path)` 打开站库。`with` 块退出及 `dispose()` 释放 SQLite 连接，设备停用通过站端 `close` 请求完成。重开保留用户录制、采集版本、票、首执和领取位置。首次初始化建立所需结构，不加载演练数据。设备原帧身份在同一站库全局唯一。设备关源后，所有已登记录制均进入关闭状态。

```python
from survey.capture.candidates import CandidateFrame, RecordingEvent
from survey.recording.coordinates import FrameWindow
from survey.recording.station import connect_station

with connect_station('station.sqlite') as station:
    window = FrameWindow(0, 2)
    station.open('pump-west', window)
    a = station.recorder.occupy('a', 7)
    b = station.recorder.occupy('b', 11)
    event = RecordingEvent((CandidateFrame(0, a), CandidateFrame(1, b)))
    accepted = station.poll('pump-west', event, 'morning-capture')
    ticket = station.issue('pump-west', accepted.edition, window, 'night-ticket')
    page = station.take(ticket.ticket_id, 'night-first', 1)
    audit = station.audit(ticket.ticket_id)
```

| 入口 | 输入与结果 |
| --- | --- |
| `open(recording, window, unit='count')` | 固定录制号、窗与原始整数口径，返回计划及当前 `active` 或 `closed` 阶段。重复登记必须使用同一计划。 |
| `poll(recording, event, operation, ...)` | 核验完整候选，返回 `PollResult`。发布时含 `edition`，待办时含 `reason`。`current_edition(recording)` 可读取最后发布的版及其成员。 |
| `cancel_overwrite(recording, operation)` | 撤回以该号登记并绑定本录制的换槽，返回 `PollResult`。设备先撤回则 `cancelled`，设备已写则 `executed`，两者 edition 均为空。未知号 `LookupError`，错绑 `RequestConflict`，普通采集号 `ValueError`。不产生新候选，也不改设备占槽。 |
| `issue(recording, edition, window, operation)` | 按已接纳版冻结票窗，返回 `IssuedTicket` 的票号、录制号、版本、窗、原首执和计量。首执为原出票操作号。历史已接纳版可出票。 |
| `take(ticket_id, operation, limit)` | 从该票已提交的偏移交付完整一页。结果含 `start`、`next_position`、`frames` 与 `sources`。耗尽后仍可请求空页，位置保持末位。 |
| `close(recording, operation)` | 关闭站端的这台录制仪，返回 `pending` 或 `closed`。全站所有历史已接纳版的原件可离线兑现后才断开。覆盖待结或原件不可读时可按原操作重办。 |
| `audit(ticket_id)` | 返回冻结计量、固定成员的出处及本次核账结果。`matches` 为真表示重读核对一致。出现差异时保留冻结值，无法完整重读时 `observed` 为空并列出差异。 |

窗口为零基半开区间，端点是可存入 SQLite 的非负整数。票窗须位于录制窗内。边界内等端点合法，帧数为零，峰值和出处为空。页量为正的 SQLite 整数。振幅是精确有符号整数，SQLite 正文及计量使用可逆字节保存，支持超过 64 位的振幅。峰值取固定成员的绝对振幅最大值，同峰取最早设备原序。设备出处含发生号、设备原序和实际物理槽。录制号、操作号及槽名为非空文本，首尾不能有空白。

## 设备事件及重办

`recorder.occupy(slot, amplitude, checksum=...)` 为未占用的槽写入采样，通常省略校验值以使用正文的 SHA-256。演练可显式提供错误校验以检验拒收。`recorder.read_occurrence(slot, occurrence_id)` 核对指定槽的身份和正文，覆写或断开后无法从该端口重取已离槽的正文。`recorder.operation_outcome(operation)` 返回已实际完成的换槽结局，持久记录包含被替换身份和新发生的出处及正文校验。

`RecordingEvent.frames` 是按窗位声明的完整有序候选。每个窗位须显式给出原帧，保留旧帧也要在本次声明。设备原序严格递增，可以有间隔。换槽候选用 `OverwriteRequest(position, slot, expected_occurrence, amplitude, checksum)` 声明最后一个窗位的待采样，其他窗位仍由 `frames` 逐项声明。最后窗位使用实际设备结局中的新发生，因为新发生晚于候选中已有的发生。`frame_checksum(amplitude)` 可从 `survey.device.readout` 导入。已占槽写入由站端 `poll` 放行，设备覆盖方法是该流程调用的设备端口。

`poll` 的确定性演练选项 `fail_custody_commit` 在保管事务提交前触发回滚，`pause_before_overwrite` 在持久准备后停在覆盖前，`lose_overwrite_reply` 在实际覆盖提交后丢失本次回包。这些选项控制单次调用，不属于重办参数。待办操作重办时重新提交同一事件和操作号。站库保存原声明的发生号、位置、出处和正文校验作为请求依据，设备结局记录保留实际发生的身份及校验。已发布版本的可兑现正文由源槽或保管库负责。同录制后来的采集候选或换槽在上一换槽结清前返回待办，上一已发布版仍可用于出票及续领；不同录制且原件与目标槽无冲突的候选照常接纳和发布，跨录制覆盖同一物理槽前先把在办候选的显式原帧、已写未发的新帧及已发布原件持久保全，或待义务解除。

`cancel_overwrite` 的终局独立于站端结算：设备先撤回时，迟到的原命令不再写槽或消耗发生序，候选没有新版；设备已写（含回包丢失）时返回 `executed`，原 `poll` 续办并采用实际发生。撤回结果按原号持久保存，重开后原号可复领；站端结算失败不留下新可领版，重开认回设备终局再续办。

坏正文、矛盾身份、缺档及非法参数抛出 `ValueError`，请求冲突抛出其子类 `RequestConflict`。未知成果或不可用原件以 `LookupError` 或其子类 `FrameUnavailable` 表示。`poll`、`issue`、`take` 和 `close` 各自使用独立操作号命名空间。同接口同号同参重办复用原结果，同号异参拒绝。`take` 的原页可回放，只有新成功操作推进位置。读取失败或 SQLite 写入失败会使当前页事务回滚；重办继续使用原偏移。SQLite 存储异常向调用者报告，操作是否已提交可以通过原请求重办查明。
