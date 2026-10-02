"""失败截图的**读取**侧测试 —— `GET /tasks/{id}/executions/{eid}/screenshot`。

和上传侧（`test_rpa_api.py` 里的 §10.4）分开，因为两者的调用方不同：
上传的是 Worker（`/rpa/*`，Worker 角色），读图的是后台管理员（管理员角色）。
合在一个文件里，读图用例就得跟一堆 Worker 夹具纠缠。

这个端点存在**本身就是一条设计决定**：截图刻意不放在静态目录，因为图里是
客户姓名/电话/地址明文，而订单列表是特意给手机号脱敏的。所以这里的核心
断言有两个 —— 「管理员拿得到」，以及「不鉴权 / 非管理员拿不到」。
"""

from datetime import datetime

import pytest

from app.core.config import settings
from app.core.enums import TaskStatus
from app.models.task_execution import TaskExecution

PREFIX = "/api/v1"
_PNG = b"\x89PNG\r\n\x1a\n-fake-image-bytes"


async def _task_with_screenshot(
    db_session, make_order, make_task, *, screenshot_path: str | None, write_file: bool,
    tmp_path,
):
    """造一个任务 + 一条执行记录，可选地在磁盘上放一张真的截图。

    `screenshot_path` 与 `write_file` 分开给，是为了造出「库里有路径、
    磁盘上没文件」这种不一致状态（运维删了文件、或者换机器没同步过来）。
    """
    order = await make_order()
    task = await make_task(order, status=TaskStatus.SUCCESS.value)
    execution = TaskExecution(
        task_id=task.id,
        attempt=1,
        status="FAILED",
        worker_name="rpa-worker-01",
        finished_at=datetime.now(),
        screenshot_path=screenshot_path,
    )
    db_session.add(execution)
    await db_session.commit()

    if write_file and screenshot_path:
        (tmp_path / screenshot_path).write_bytes(_PNG)
    return task, execution


def _url(task_id: int, execution_id: int) -> str:
    return f"{PREFIX}/tasks/{task_id}/executions/{execution_id}/screenshot"


@pytest.mark.integration
async def test_admin_can_download_the_screenshot(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path="7.png", write_file=True, tmp_path=tmp_path,
    )

    response = await api_client.get(_url(task.id, execution.id), headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    # 二进制响应**不套** {code,message,data} 外壳 —— 那层壳装不下图片字节
    assert response.content == _PNG


@pytest.mark.integration
async def test_screenshot_download_requires_authentication(
    api_client, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path="7.png", write_file=True, tmp_path=tmp_path,
    )

    response = await api_client.get(_url(task.id, execution.id))

    assert response.status_code == 401
    assert response.json()["code"] == 4001


@pytest.mark.integration
async def test_screenshot_download_rejects_the_worker_role(
    api_client, worker_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    """Worker 传得上去，但读不回来 —— 它不需要回头看别人（或自己）的截图。"""
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path="7.png", write_file=True, tmp_path=tmp_path,
    )

    response = await api_client.get(
        _url(task.id, execution.id), headers=worker_headers
    )

    assert response.status_code == 403
    assert response.json()["code"] == 4003


@pytest.mark.integration
async def test_screenshot_download_404s_when_the_execution_has_no_screenshot(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path=None, write_file=False, tmp_path=tmp_path,
    )

    response = await api_client.get(_url(task.id, execution.id), headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == 4004


@pytest.mark.integration
async def test_screenshot_download_404s_when_the_file_is_gone(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    """库里有文件名、磁盘上没这个文件 —— 报「截图文件已丢失」，不是 500。"""
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path="7.png", write_file=False, tmp_path=tmp_path,
    )

    response = await api_client.get(_url(task.id, execution.id), headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == 4004


@pytest.mark.integration
async def test_screenshot_download_404s_when_the_execution_belongs_to_another_task(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    """执行记录 id 对、但 task_id 对不上 —— 不能靠猜 id 读到别人的执行。"""
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    _, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path="7.png", write_file=True, tmp_path=tmp_path,
    )
    other_order = await make_order()
    other_task = await make_task(other_order, status=TaskStatus.SUCCESS.value)

    response = await api_client.get(
        _url(other_task.id, execution.id), headers=auth_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4004


@pytest.mark.integration
async def test_screenshot_download_strips_path_traversal_from_the_stored_name(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    """`screenshot_path` 是我们自己生成的文件名，但仍按「不可信」处理。

    真把这么一个值写进库（比如以后有人从别处灌数据），这一行就挡住了
    `../../etc/passwd` 这类路径穿越 —— 取的是 `Path(...).name`，
    目录成分全被剥掉，落到截图目录里找不到，于是 404 而不是读出系统文件。
    """
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("should never be served")
    task, execution = await _task_with_screenshot(
        db_session, make_order, make_task,
        screenshot_path=f"../{outside.name}", write_file=False, tmp_path=tmp_path,
    )

    response = await api_client.get(_url(task.id, execution.id), headers=auth_headers)

    assert response.status_code == 404
    assert "should never be served" not in response.text
