"""任务领域的异常类型：谁都要抛/接它们，所以单独一层，不给依赖图添环。

- :class:`JobCancelled`：协作式取消（下一次进度检查点抛）；
- :class:`UploadRejected`：上传被拒（后缀/大小/空文件/不可播）；
- :class:`StageNotReady`：阶段前置条件没满足（还没扒谱就想跑歌词）；
- :class:`ImportedProject`：`output/` 里别人的产物，只读；
- :class:`StaleRevision`：乐观锁冲突（改参数时这页已经旧了）。

API 层把这几个翻译成对应状态码（见 `routes/_shared.py`），别在路由里重复判断。
"""

from __future__ import annotations

class JobCancelled(RuntimeError):
    """协作式取消：在下一次进度检查点抛出。"""


class UploadRejected(ValueError):
    """上传被拒（后缀不对 / 太大 / 空文件）。"""


class StageNotReady(RuntimeError):
    """阶段的前置条件没满足（例：还没扒谱就想跑歌词识别）。

    API 把它翻成 409 + 一句人话（"请先完成扒谱"），界面直接显示这句。
    """


class ImportedProject(RuntimeError):
    """"这是 `output/` 里已经存在的产物，不是 WebUI 建的工程"。

    工程列表**以 `output/` 为准**：别人（或命令行）跑出来的目录也会列进侧栏，
    能看卷帘 / 下载 / 导出 SVP，但没有原音频、也不知道当时的参数，所以**不能重跑**。
    API 把它翻成 409 + 一句人话。
    """


class StaleRevision(RuntimeError):
    """乐观锁冲突（G8）：改参数时带了 `expected_rev`，但工程的 `rev` 已经变了。

    意味着"你是基于旧快照改的"，服务端不覆盖，而是回 409 + 当前快照，让前端刷新。
    `current_rev` 是服务端此刻的版本，前端拿它提示用户"这页是旧的"。
    """

    def __init__(self, current_rev: int) -> None:
        self.current_rev = int(current_rev)
        super().__init__(
            f"这页是旧的（当前已经是第 {self.current_rev} 版），已帮你刷新，请重试")
